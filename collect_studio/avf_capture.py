"""AVFoundation 直采:按 uniqueID 打开相机,彻底绕开 OpenCV 的 index 枚举。

背景:OpenCV(CAP_AVFOUNDATION)内部的设备顺序与任何 pyobjc 枚举 API 的顺序
都不保证一致(实测确实漂移),按 index 采集会出现"名字/uniqueID 与画面错位"。
本模块用 AVCaptureSession + deviceWithUniqueID: 直接开设备 —— 身份与画面天然同源。
"""
import logging
import threading
import time

import cv2
import numpy as np
import objc
import AVFoundation as AVF
import CoreMedia
import libdispatch
from Foundation import NSObject
from Quartz import CoreVideo as CV

log = logging.getLogger("avf_capture")

_LOCK_READONLY = getattr(CV, "kCVPixelBufferLock_ReadOnly", 1)


class _SampleSink(NSObject):
    """AVCaptureVideoDataOutputSampleBufferDelegate → numpy BGR 回调。"""

    def initWithOwner_(self, owner):
        self = objc.super(_SampleSink, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    def captureOutput_didOutputSampleBuffer_fromConnection_(self, output, sample_buffer, connection):
        owner = self._owner
        if owner is None:
            return
        try:
            img = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
            if img is None:
                return
            CV.CVPixelBufferLockBaseAddress(img, _LOCK_READONLY)
            try:
                w = CV.CVPixelBufferGetWidth(img)
                h = CV.CVPixelBufferGetHeight(img)
                bpr = CV.CVPixelBufferGetBytesPerRow(img)
                base = CV.CVPixelBufferGetBaseAddress(img)
                buf = base.as_buffer(bpr * h)
                arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, bpr)
                frame = arr[:, : w * 4].reshape(h, w, 4)[:, :, :3].copy()  # BGRA → BGR
            finally:
                CV.CVPixelBufferUnlockBaseAddress(img, _LOCK_READONLY)
            owner._on_frame(frame)
        except Exception as e:  # noqa: BLE001  回调在 dispatch 线程,绝不能抛
            owner._on_callback_error(e)


class AVFCamStream:
    """按 uniqueID 采集;接口与旧 CamStream 兼容(latest / latest_jpeg / ok / err / stop)。"""

    def __init__(self, unique_id: str, width: int, height: int, fps: int):
        self.unique_id = unique_id
        self.width, self.height, self.fps = width, height, fps
        self.frame = None
        self.frame_ts = 0.0
        self.frame_count = 0
        self.ok = False
        self.err: str | None = None
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._session = None
        self._sink = None
        self._queue = None
        self._stopped = False
        self._start_thread = threading.Thread(
            target=self._start_safe, name=f"camera-start-{unique_id}", daemon=True)
        self._start_thread.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---------- 内部 ----------
    def _start_safe(self):
        try:
            self._start()
        except Exception as e:  # noqa: BLE001
            log.exception("AVF start failed for %s", self.unique_id)
            with self._lock:
                if not self._stopped:
                    self.err = f"启动采集失败:{e}"

    def _start(self):
        dev = AVF.AVCaptureDevice.deviceWithUniqueID_(self.unique_id)
        if dev is None:
            self.err = "设备不存在(可能已拔出)"
            return
        inp, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(dev, None)
        if inp is None:
            self.err = f"打开设备失败:{err}"
            return
        session = AVF.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        preset = AVF.AVCaptureSessionPreset640x480
        if session.canSetSessionPreset_(preset):
            session.setSessionPreset_(preset)
        if not session.canAddInput_(inp):
            session.commitConfiguration()
            self.err = "无法添加输入(权限未授予或设备被占用)"
            return
        session.addInput_(inp)
        out = AVF.AVCaptureVideoDataOutput.alloc().init()
        out.setVideoSettings_({CV.kCVPixelBufferPixelFormatTypeKey: CV.kCVPixelFormatType_32BGRA})
        out.setAlwaysDiscardsLateVideoFrames_(True)
        self._sink = _SampleSink.alloc().initWithOwner_(self)
        self._queue = libdispatch.dispatch_queue_create(
            f"collect-studio.cam.{self.unique_id}".encode(), None)
        out.setSampleBufferDelegate_queue_(self._sink, self._queue)
        if not session.canAddOutput_(out):
            session.commitConfiguration()
            self.err = "无法添加输出"
            return
        session.addOutput_(out)
        session.commitConfiguration()
        try:  # 帧率(尽力而为,失败不致命)
            ok, _ = dev.lockForConfiguration_(None)
            if ok:
                dur = CoreMedia.CMTimeMake(1, self.fps)
                dev.setActiveVideoMinFrameDuration_(dur)
                dev.setActiveVideoMaxFrameDuration_(dur)
                dev.unlockForConfiguration()
        except Exception:  # noqa: BLE001
            pass
        if self._stopped:
            return
        session.startRunning()
        with self._lifecycle_lock:
            if self._stopped:
                session.stopRunning()
            else:
                self._session = session

    def _on_frame(self, frame):
        if self._stopped:
            return
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height))
        with self._lock:
            self.frame = frame
            self.frame_ts = time.time()
            self.frame_count += 1
            self.ok = True
            self.err = None

    def _on_callback_error(self, error):
        with self._lock:
            if not self._stopped and not self.ok:
                self.err = f"读取画面失败:{error}"

    def _watchdog(self):
        """3 秒收不到帧标记为异常(权限/占用/拔出),恢复后自动转好。"""
        while not self._stopped:
            time.sleep(1.0)
            if self.frame_ts and time.time() - self.frame_ts > 3.0:
                self.ok = False
                if not self.err:
                    self.err = "超过 3s 未收到帧"
            elif not self.frame_ts and not self.err:
                self.err = "等待首帧…(若持续,检查摄像头权限)"

    # ---------- 对外接口(与旧 CamStream 一致) ----------
    def latest(self):
        with self._lock:
            return None if self.frame is None else self.frame.copy()

    def latest_jpeg(self, quality=70):
        f = self.latest()
        if f is None:
            return None
        ok, buf = cv2.imencode(".jpg", f, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    def stop(self):
        with self._lifecycle_lock:
            self._stopped = True
            session, self._session = self._session, None
        try:
            if session is not None:
                session.stopRunning()
        except Exception:  # noqa: BLE001
            pass
        if self._sink is not None:
            self._sink._owner = None
