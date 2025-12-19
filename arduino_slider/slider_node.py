#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import time
import threading
from collections import deque

import serial  # pip install pyserial が必要
import rclpy
from rclpy.node import Node
from std_msgs.msg import Int32, Float32

# --- 送信方式を選ぶ ---
# 2バイト生データ (little endian) なら True
# Serial.println(1234) みたいなテキスト送信なら False
DEFAULT_USE_BINARY = False

# ASCII のときのボーレート
BAUD_ASCII = 115200

# Binary のときのボーレート（mira3.py では 250000）
BAUD_BINARY = 250000


class SerialReaderBin:
    """
    Arduino 側が 0〜65535 を 2バイト little-endian で垂れ流す想定の高速リーダー。
    """
    def __init__(self, port: str, baud: int = BAUD_BINARY):
        self.port = port
        self.baud = baud
        self.ser = None
        self.latest = None  # 最新値だけ保持
        self._stop = False
        self._th = None

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
                    # 2バイトずつ読む
                    while len(buf) >= 2:
                        v = buf[0] | (buf[1] << 8)  # little-endian
                        del buf[:2]
                        self.latest = v
            except Exception:
                pass
            time.sleep(0.001)  # CPU 負荷軽減

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

        # パラメータ
        self.declare_parameter("port", "/dev/ttyACM0")
        self.declare_parameter("use_binary", DEFAULT_USE_BINARY)
        self.declare_parameter("publish_rate_hz", 50.0)
        self.declare_parameter("smoothing_alpha", 0.5)
        self.declare_parameter("max_value", 1023.0)

        self.port = self.get_parameter("port").get_parameter_value().string_value
        self.use_binary = self.get_parameter("use_binary").get_parameter_value().bool_value
        self.publish_rate_hz = self.get_parameter("publish_rate_hz").get_parameter_value().double_value
        self.alpha = self.get_parameter("smoothing_alpha").get_parameter_value().double_value
        self.max_value = self.get_parameter("max_value").get_parameter_value().double_value

        # パブリッシャ
        self.pub_raw = self.create_publisher(Int32, "slider_raw", 10)
        self.pub_smooth = self.create_publisher(Float32, "slider_smooth", 10)

        # 内部状態
        self.latest_raw = None
        self.smooth_val = None

        # シリアル初期化
        if self.use_binary:
            self.get_logger().info(
                f"Using BINARY mode on {self.port} @ {BAUD_BINARY} (2-byte little-endian)"
            )
            self.reader_bin = SerialReaderBin(self.port, BAUD_BINARY)
            self.reader_bin.start()
            self.ser_ascii = None
        else:
            self.get_logger().info(
                f"Using ASCII mode on {self.port} @ {BAUD_ASCII} (Serial.println)"
            )
            self.reader_bin = None
            self.ser_ascii = self._open_ascii()

        # タイマーで定期実行
        dt = 1.0 / self.publish_rate_hz if self.publish_rate_hz > 0.0 else 0.02
        self.timer = self.create_timer(dt, self.timer_callback)

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

        if self.ser_ascii is None:
            return None

        latest = None
        try:
            while True:
                line = self.ser_ascii.readline().decode("utf-8", errors="ignore").strip()
                if not line:
                    break  # これ以上読むものがなければ終了
                try:
                    v = int(line)
                    latest = v  # 成功したものを「最新」として記録
                except ValueError:
                    # 数字でない行は無視して次へ
                    continue
        except Exception as e:
            self.get_logger().warn(f"[ArduinoAscii] read error: {e}")
            return None

        return latest  # 読めた中で一番新しい値（無ければ None）


    # --- タイマーコールバック: 定期的に値を読んで publish ---
    def timer_callback(self):
        # 1) 値を取得
        if self.use_binary:
            v = self.reader_bin.get() if self.reader_bin is not None else None
        else:
            v = self._read_ascii_once()

        if v is None:
            # 読めなかった瞬間は publish をスキップ
            return

        self.latest_raw = int(v)

        # 2) 簡単な平滑化（IIR: smooth = alpha * new + (1-alpha) * old）
        if self.smooth_val is None:
            self.smooth_val = float(self.latest_raw)
        else:
            a = float(self.alpha)
            self.smooth_val = (1.0 - a) * self.smooth_val + a * float(self.latest_raw)

        # 3) publish (raw)
        msg_raw = Int32()
        msg_raw.data = self.latest_raw
        self.pub_raw.publish(msg_raw)

        # 4) publish (smooth)
        msg_smooth = Float32()
        msg_smooth.data = float(self.smooth_val)
        self.pub_smooth.publish(msg_smooth)

        # デバッグ用（うるさければコメントアウト）
        #self.get_logger().info(f"raw={self.latest_raw}, smooth={self.smooth_val:.1f}")

    def destroy_node(self):
        self.get_logger().info("Shutting down slider_node...")
        try:
            if self.reader_bin is not None:
                self.reader_bin.stop()
        except Exception:
            pass
        if hasattr(self, "ser_ascii") and self.ser_ascii is not None:
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
