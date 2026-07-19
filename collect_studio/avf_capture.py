"""AVFoundation 直采:按 uniqueID 打开相机,彻底绕开 OpenCV 的 index 枚举。

背景:OpenCV(CAP_AVFOUNDATION)内部的设备顺序与任何 pyobjc 枚举 API 的顺序
都不保证一致(实测确实漂移),按 index 采集会出现"名字/uniqueID 与画面错位"。
本模块用 AVCaptureSession + deviceWithUniqueID: 直接开设备 —— 身份与画面天然同源。

多路同开的实测结论(2026-07,三台 UVC 相机挂同一 USB 控制器):
- 这些相机只支持无压缩格式(420v/yuvs,没有 MJPEG),640x480@30 每路 ~14MB/s,
  而帧率若不加约束会被 AVCaptureSession 按格式支持的最高率(60fps)协商,
  单路带宽直接翻倍 → 前两路占满等时带宽,第三路一帧都收不到。
- 因此必须显式 setActiveFormat_ + 钳制帧率;且钳制必须用 AVFrameRateRange 自带的
  CMTime(UVC 驱动做精确匹配,CMTimeMake(1,30) 会被拒抛 NSInvalidArgumentException)。
- 串行启动 + 等首帧:同一时间只做一路的带宽协商,失败可整路重试。
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

# 同一时间只允许一路做启动(带宽协商),其余排队 —— 见模块 docstring 的实测结论
_START_LOCK = threading.Lock()
_WARMUP_S = 3.0        # 每路启动后等首帧的最长时长
_START_ATTEMPTS = 3    # 收不到帧时整路重建的最多次数
_RETRY_BACKOFF_S = 0.5


def _pick_format_and_fps_range(dev, width, height, fps):
    """在设备格式列表里找精确匹配 width×height 且支持 fps 的 (format, frameRateRange)。

    找不到完全匹配时返回 (None, None),回退到 session preset 自动协商(旧行为)。
    """
    for fmt in dev.formats():
        dims = CoreMedia.CMVideoFormatDescriptionGetDimensions(fmt.formatDescription())
        if (int(dims.width), int(dims.height)) != (width, height):
            continue
        for r in fmt.videoSupportedFrameRateRanges():
            if r.minFrameRate() <= fps <= r.maxFrameRate():
                return fmt, r
    return None, None


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
        self.started_at = time.time()  # 供外层判断"还在启动重试中",别急着重建
        self._start_thread = threading.Thread(
            target=self._start_safe, name=f"camera-start-{unique_id}", daemon=True)
        self._start_thread.start()
        threading.Thread(target=self._watchdog, daemon=True).start()

    # ---------- 内部 ----------
    def _start_safe(self):
        """串行启动 + 等首帧(warmup)+ 整路重试;彻底失败才置 err。

        串行(_START_LOCK):同一时间只做一路的 USB 带宽协商,多路并发抢占时
        后启动的会一帧都收不到;warmup 内收到首帧才算成功,否则重建整路重试。
        """
        with _START_LOCK:
            for attempt in range(1, _START_ATTEMPTS + 1):
                if self._stopped:
                    return
                with self._lock:
                    self.err = None
                try:
                    self._start()
                except Exception as e:  # noqa: BLE001
                    log.exception("AVF start failed for %s", self.unique_id)
                    with self._lock:
                        self.err = f"启动采集失败:{e}"
                if not self._stopped and self._session is not None:
                    deadline = time.time() + _WARMUP_S
                    while not self._stopped and not self.frame_ts and time.time() < deadline:
                        time.sleep(0.05)
                    if self.frame_ts:
                        if attempt > 1:
                            log.info("camera %s 第 %d 次尝试后出图", self.unique_id, attempt)
                        return
                    log.warning("camera %s 第 %d/%d 次启动 %ss 无帧,重建(可能 USB 带宽不足)",
                                self.unique_id, attempt, _START_ATTEMPTS, _WARMUP_S)
                self._stop_session()
                time.sleep(_RETRY_BACKOFF_S)
            with self._lock:
                if not self._stopped and not self.err:
                    self.err = "多次启动均未收到画面(可能 USB 带宽不足或设备被占用)"

    def _start(self):
        dev = AVF.AVCaptureDevice.deviceWithUniqueID_(self.unique_id)
        if dev is None:
            self.err = "设备不存在(可能已拔出)"
            return
        # 显式选格式并把帧率钳到 self.fps —— 不做的话 session 会按该格式支持的最高
        # 帧率(实测 60fps)协商,单路 USB 带宽翻倍,多路同开时后启动的收不到帧。
        # 必须在建 session 之前做(session 一旦按 preset 启动就会重协商覆盖设备设置)。
        fmt, fps_range = _pick_format_and_fps_range(dev, self.width, self.height, self.fps)
        if fmt is not None:
            ok, _ = dev.lockForConfiguration_(None)
            if ok:
                # UVC 驱动对帧时长做精确匹配,必须用 range 自带的 CMTime;
                # 自己 CMTimeMake(1, fps) 会被拒(实测抛 NSInvalidArgumentException)
                dev.setActiveFormat_(fmt)
                dev.setActiveVideoMinFrameDuration_(fps_range.maxFrameDuration())
                dev.setActiveVideoMaxFrameDuration_(fps_range.minFrameDuration())
                dev.unlockForConfiguration()
        inp, err = AVF.AVCaptureDeviceInput.deviceInputWithDevice_error_(dev, None)
        if inp is None:
            self.err = f"打开设备失败:{err}"
            return
        session = AVF.AVCaptureSession.alloc().init()
        session.beginConfiguration()
        if fmt is None:
            # 回退:设备没有精确的 640x480 格式,交给 preset 自动协商(可能拿到更高帧率)
            preset = AVF.AVCaptureSessionPreset640x480
            if session.canSetSessionPreset_(preset):
                session.setSessionPreset_(preset)
        elif session.canSetSessionPreset_(AVF.AVCaptureSessionPresetInputPriority):
            # 已显式设过 activeFormat,告诉 session 不要再用 preset 覆盖设备格式
            session.setSessionPreset_(AVF.AVCaptureSessionPresetInputPriority)
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

    def _stop_session(self):
        """停掉当前 session 并清空引用(不置 _stopped,供启动失败重试复用)。"""
        with self._lifecycle_lock:
            session, self._session = self._session, None
        try:
            if session is not None:
                session.stopRunning()
        except Exception:  # noqa: BLE001
            pass

    def stop(self):
        with self._lifecycle_lock:
            self._stopped = True
        self._stop_session()
        if self._sink is not None:
            self._sink._owner = None
