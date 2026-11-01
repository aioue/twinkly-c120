"""Encoder and mock-controller checks. No real string is contacted."""

from __future__ import annotations

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import c120


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        server = self.server
        assert isinstance(server, Server)
        server.calls.append((self.command, self.path))
        if self.path == "/xled/v1/gestalt":
            self._json(dict(server.gestalt))
            return
        if self.path == "/xled/v1/fw/version":
            self._json({"version": "2.9.1", "code": 1000})
            return
        if self.path in {"/xled/v1/movies", "/xled/v1/led/mode"}:
            if not self.headers.get("X-Auth-Token"):
                self._send(401, b"Invalid Token")
                return
            if self.path == "/xled/v1/movies":
                self._json(
                    {
                        "movies": list(server.movies),
                        "available_frames": server.available_frames,
                        "code": 1000,
                    }
                )
                return
            self._json({"mode": server.mode, "code": 1000})
            return
        self._send(404, b"missing")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        server = self.server
        assert isinstance(server, Server)
        server.calls.append((self.command, self.path))
        if self.path == "/xled/v1/login":
            token = base64.b64encode(b"01234567").decode("ascii")
            self._json(
                {
                    "authentication_token": token,
                    "authentication_token_expires_in": 14400,
                    "challenge-response": "abc",
                    "code": 1000,
                }
            )
            return
        if not self.headers.get("X-Auth-Token"):
            self._send(401, b"Invalid Token")
            return
        if self.path == "/xled/v1/verify":
            self._json({"code": 1000})
            return
        if self.path == "/xled/v1/movies/full":
            if server.fail_full:
                self._send(500, b"flash failed")
                return
            server.movie = body
            stored = {
                "id": 0,
                "name": "C.120",
                "descriptor_type": "rgbw_raw",
                "leds_per_frame": 4,
                "frames_number": 1 if server.bad_readback else c120.FRAMES,
                "fps": 17,
            }
            server.movies = [stored]
            self._json({"code": 1000})
            return
        if body and self.headers.get("Content-Type", "").startswith("application/json"):
            server.json_posts[self.path] = json.loads(body)
        if self.path == "/xled/v1/led/mode":
            server.mode = json.loads(body)["mode"]
        self._json({"code": 1000})

    def _json(self, payload: dict) -> None:
        self._send(200, json.dumps(payload).encode("utf-8"))

    def _send(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.calls: list[tuple[str, str]] = []
        self.json_posts: dict[str, dict] = {}
        self.movie = b""
        self.movies: list[dict] = []
        self.available_frames = 4026
        self.mode = "effect"
        self.fail_full = False
        self.bad_readback = False
        self.gestalt = {
            "product_code": "TWS250SPP2",
            "hardware_version": "307",
            "number_of_led": 4,
            "movie_capacity": 4026,
            "bytes_per_led": 4,
            "led_profile": "RGBW",
            "compatibility_mode": "enabled",
            "max_supported_led": 510,
            "code": 1000,
        }


class EncoderTests(unittest.TestCase):
    def test_preset_numbers(self) -> None:
        self.assertEqual(c120.FRAMES, 2160)
        self.assertEqual(c120.FPS, 18)
        self.assertEqual(c120.FRAME_DELAY_MS, 56)

    def test_first_led_is_red_and_white_stays_off(self) -> None:
        frame = c120.render_frame(250, 0.0)
        self.assertEqual(frame[:4], bytes((0, 255, 0, 0)))
        self.assertEqual(frame[125 * 4 : 126 * 4], frame[:4])
        movie = c120.encode_movie(250)
        self.assertEqual(len(movie), 2160 * 1000)
        self.assertTrue(all(movie[index] == 0 for index in range(0, len(movie), 4)))

    def test_loop_does_not_repeat_the_first_frame(self) -> None:
        first = c120.render_frame(250, 0.0)
        last = c120.render_frame(250, 2159 / 2160)
        self.assertNotEqual(first, last)
        self.assertEqual(first, c120.render_frame(250, 1.0))


class UploadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = Server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_plan_stays_offline(self) -> None:
        self.assertEqual(c120.main(["plan"]), 0)
        self.assertEqual(self.server.calls, [])

    def test_upload_without_yes_does_not_write(self) -> None:
        code = c120.main(["upload", "127.0.0.1", "--port", str(self.port)])
        self.assertEqual(code, 0)
        posts = [path for method, path in self.server.calls if method == "POST"]
        self.assertEqual(posts, ["/xled/v1/login", "/xled/v1/verify"])
        self.assertEqual(self.server.movie, b"")

    def test_yes_uploads_one_movie_and_plays_it(self) -> None:
        code = c120.main(["upload", "127.0.0.1", "--yes", "--port", str(self.port)])
        self.assertEqual(code, 0)
        created = self.server.json_posts["/xled/v1/movies/new"]
        self.assertEqual(created["descriptor_type"], "rgbw_raw")
        self.assertEqual(created["fps"], 18)
        self.assertEqual(created["frames_number"], 2160)
        self.assertEqual(created["leds_per_frame"], 4)
        self.assertEqual(created["name"], "C.120")
        self.assertEqual(len(self.server.movie), 2160 * 4 * 4)
        self.assertEqual(self.server.json_posts["/xled/v1/led/movie/config"]["frame_delay"], 56)
        self.assertEqual(self.server.json_posts["/xled/v1/led/mode"], {"mode": "movie"})
        self.assertEqual(
            self.server.json_posts["/xled/v1/led/out/brightness"],
            {"mode": "enabled", "type": "A", "value": 80},
        )
        self.assertNotIn("DELETE", [method for method, _ in self.server.calls])
        self.assertNotIn("/xled/v1/fw/update", [path for _, path in self.server.calls])

    def test_existing_movie_is_not_touched(self) -> None:
        self.server.movies = [{"id": 0, "name": "keep"}]
        code = c120.main(["upload", "127.0.0.1", "--yes", "--port", str(self.port)])
        self.assertEqual(code, 1)
        self.assertNotIn("/xled/v1/movies/new", [path for _, path in self.server.calls])
        self.assertEqual(self.server.mode, "effect")

    def test_rgb_string_is_refused_before_login(self) -> None:
        self.server.gestalt["led_profile"] = "RGB"
        self.server.gestalt["bytes_per_led"] = 3
        code = c120.main(["upload", "127.0.0.1", "--yes", "--port", str(self.port)])
        self.assertEqual(code, 1)
        self.assertNotIn("/xled/v1/login", [path for _, path in self.server.calls])

    def test_failed_body_does_not_start_playback(self) -> None:
        self.server.fail_full = True
        code = c120.main(["upload", "127.0.0.1", "--yes", "--port", str(self.port)])
        self.assertEqual(code, 1)
        posts = [path for method, path in self.server.calls if method == "POST"]
        self.assertNotIn("/xled/v1/led/mode", posts)
        self.assertEqual(self.server.mode, "effect")

    def test_bad_readback_does_not_start_playback(self) -> None:
        self.server.bad_readback = True
        code = c120.main(["upload", "127.0.0.1", "--yes", "--port", str(self.port)])
        self.assertEqual(code, 1)
        posts = [path for method, path in self.server.calls if method == "POST"]
        self.assertNotIn("/xled/v1/led/mode", posts)
        self.assertNotIn("/xled/v1/led/out/brightness", posts)

    def test_compatibility_mode_off_does_not_log_in(self) -> None:
        self.server.gestalt["compatibility_mode"] = "disabled"
        code = c120.main(["inspect", "127.0.0.1", "--port", str(self.port)])
        self.assertEqual(code, 1)
        self.assertNotIn("/xled/v1/login", [path for _, path in self.server.calls])


if __name__ == "__main__":
    unittest.main()
