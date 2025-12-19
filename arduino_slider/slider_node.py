#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import threading
from collections import deque

import serial  # pip install pyserial
import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32, Float32

# --- 送信方式を選ぶ ---
DEFAULT_USE_BINARY = False

# ボーレート
BAUD_ASCII = 115200
BAUD_BINARY = 250000


def _clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v


class SerialReaderBin:
    """
    Arduino 側が 0〜65535 を 2バイト little-endian で垂れ流す想定の高速リーダー。
    """
    def __init__(self, port: str, baud: int = BAUD_BINARY, sleep_sec: float = 0.001):
        self.port = port
        self.baud = baud
        self.sleep_sec = float(sleep_sec)

        self.ser = None
        self.latest = None  # 最新値だけ保持
        self._stop = False
        self._th = None

        # バッファ暴走対策（同期ズレで溜まり続けるのを防ぐ）
        self._max_buf = 8192

    def start(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0)  # 非ブロッキング
            time.sleep(2.0)  # Arduino の自動リセット待ち
            print(f"[ArduinoBin] Connected: {self.port}@{self.baud}")
        except Exception as e:
            print(f"[ArduinoBin] open failed: {e}")
            self.ser = None
            return

        self._th = threading.Thread(target=self._run, daemon=True)
        self._th.start()

    def _run(self):
        buf = bytearray()
        while not self._stop and self.ser is not None:
            try:
                chunk = self.ser.read(1024)
                if chunk:
                    buf.extend(chunk)

                    # バッファが増えすぎたら古い分を捨てる（同期ズレ対策）
                    if len(buf) > self._max_buf:
                        del buf[:-256]  # 最後の少しだけ残す

                    # 2バイトずつ読む
                    while len(buf) >= 2:
                        v = buf[0] | (buf[1] << 8)  # little-endian
                        del buf[:2]
                        self.latest = int(v)
            except Exception:
                pass

            time.sleep(self.sleep_sec)

    def get(self):
        """最新値（int or None）を返す"""
        return self.latest

    def stop(self):
        self._stop = True
        if self._th:
            self._th.join(timeout=0.2)
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser = None


class SliderNode(Node):
    """
    Arduino に接続されたスライドボリュームを読み取り、
    /slider_raw (Int32), /slider_smooth (Float32) を publish するノード
    """

    def __init__(self):
        super().__init__("slider_node")

        # ===== パラメータ =====
        self.declare_parameter("port", "/dev/ttyACM0")
        self.declare_parameter("use_binary", DEFAULT_USE_BINARY)

        self.declare_parameter("publish_rate_hz", 50.0)
        self.declare_parameter("smoothing_alpha", 0.5)
        self.declare_parameter("max_value", 1023.0)

        # topic（絶対名にして他ノードとズレにくく）
        self.declare_parameter("topic_raw", "/slider_raw")
        self.declare_parameter("topic_smooth", "/slider_smooth")

        # ASCII モードで1周期に読む最大行数（負荷抑制）
        self.declare_parameter("ascii_max_lines_per_tick", 20)

        self.port = self.get_parameter("port").value
        self.use_binary = self.get_parameter("use_binary").value
        self.publish_rate_hz = float(self.get_parameter("publish_rate_hz").value)

        self.alpha = float(self.get_parameter("smoothing_alpha").value)
        self.max_value = float(self.get_parameter("max_value").value)

        self.topic_raw = self.get_parameter("topic_raw").value
        self.topic_smooth = self.get_parameter("topic_smooth").value
        self.ascii_max_lines = int(self.get_parameter("ascii_max_lines_per_tick").value)

        # alpha の安全化（0〜1）
        self.alpha = _clamp(self.alpha, 0.0, 1.0)

        # ===== publisher =====
        self.pub_raw = self.create_publisher(Int32, self.topic_raw, 10)
        self.pub_smooth = self.create_publisher(Float32, self.topic_smooth, 10)

        # ===== 内部状態 =====
        self.latest_raw = None
        self.smooth_val = None

        # ===== シリアル初期化 =====
        self.reader_bin = None
        self.ser_ascii = None

        if self.use_binary:
            self.get_logger().info(
                f"Using BINARY mode on {self.port} @ {BAUD_BINARY} (2-byte little-endian)"
            )
            self.reader_bin = SerialReaderBin(self.port, BAUD_BINARY)
            self.reader_bin.start()
        else:
            self.get_logger().info(
                f"Using ASCII mode on {self.port} @ {BAUD_ASCII} (Serial.println)"
            )
            self.ser_ascii = self._open_ascii()

        # ===== timer =====
        dt = 1.0 / self.publish_rate_hz if self.publish_rate_hz > 0.0 else 0.02
        self.timer = self.create_timer(dt, self.timer_callback)

        self.get_logger().info(
            f"Publish: raw={self.topic_raw}, smooth={self.topic_smooth}, rate={1.0/dt:.1f}Hz, alpha={self.alpha:.2f}"
        )

    # --- ASCII モード用 ---
    def _open_ascii(self):
        try:
            ser = serial.Serial(self.port, baudrate=BAUD_ASCII, timeout=0.0)
            time.sleep(2.0)  # 自動リセット待ち
            self.get_logger().info(f"[ArduinoAscii] Connected: {ser.port}")
            return ser
        except Exception as e:
            self.get_logger().error(f"[ArduinoAscii] open failed: {e}")
            return None

    def _read_ascii_once(self):
        """
        1タイマー周期で最大 ascii_max_lines 行だけ読む。
        読めた中で一番新しい int を返す（無ければ None）
        """
        if self.ser_ascii is None:
            return None

        latest = None
        try:
            for _ in range(max(self.ascii_max_lines, 1)):
                line = self.ser_ascii.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    break
                try:
                    latest = int(line)
                except ValueError:
                    continue
        except Exception as e:
            # 連続ログを避けたいなら throttle を入れてもよい
            self.get_logger().warning(f"[ArduinoAscii] read error: {e}")
            return None

        return latest

    def _get_value(self):
        if self.use_binary:
            return self.reader_bin.get() if self.reader_bin is not None else None
        return self._read_ascii_once()

    def timer_callback(self):
        v = self._get_value()
        if v is None:
            return

        # 0〜max_value でクリップ（ノイズ/異常値対策）
        raw = int(_clamp(float(v), 0.0, self.max_value))
        self.latest_raw = raw

        # IIR 平滑化
        if self.smooth_val is None:
            self.smooth_val = float(raw)
        else:
            a = self.alpha
            self.smooth_val = (1.0 - a) * self.smooth_val + a * float(raw)

        # publish raw
        msg_raw = Int32()
        msg_raw.data = raw
        self.pub_raw.publish(msg_raw)

        # publish smooth
        msg_smooth = Float32()
        msg_smooth.data = float(self.smooth_val)
        self.pub_smooth.publish(msg_smooth)

    def destroy_node(self):
        self.get_logger().info("Shutting down slider_node...")
        try:
            if self.reader_bin is not None:
                self.reader_bin.stop()
        except Exception:
            pass

        if self.ser_ascii is not None:
            try:
                self.ser_ascii.close()
            except Exception:
                pass

        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = SliderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
