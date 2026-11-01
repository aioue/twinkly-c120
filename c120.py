#!/usr/bin/env python3
"""Upload the C.120 rainbow movie to one Twinkly RGBW string.

The movie is 2160 full-range HSV frames: two rainbows along the string, one
revolution every 120 seconds if the controller plays the requested 18 fps.
The white byte stays 0. Brightness is the controller dimmer at 80%, not a
darker file.

This file never calls movie delete, firmware update, reset, or network
configuration. A string that already has a movie is left alone, including
when an earlier upload created a slot and then failed. Clearing that slot
is the Twinkly app's job.

``movies/new`` takes an integer fps. ``led/movie/config`` takes ``frame_delay``
in milliseconds. Both are sent. On a TWS250SPP2 at firmware 2.9.1 the stored
fps came back as 17 while frame_delay stayed 56. Which field the player
obeys was not timed, so a successful upload is not a measured 120 second loop.
"""

from __future__ import annotations

import argparse
import base64
import colorsys
import json
import math
import os
import sys
import urllib.error
import urllib.request
import uuid

# C.120, the preset that was stored and left playing on a 250 RGBW string.
REVOLUTION_SECONDS = 120
FPS = 18
FRAMES = int(round(REVOLUTION_SECONDS * FPS))
FRAME_DELAY_MS = int(round(1000 / FPS))
RAINBOW_COUNT = 2
BRIGHTNESS_PERCENT = 80
MOVIE_NAME = "C.120"

# A corrupt gestalt should not make this allocate a huge frame buffer.
MAX_LEDS = 4096
JSON_LIMIT = 1_000_000
# Flash write on the controller is slower than the LAN copy of the file.
UPLOAD_TIMEOUT = 180.0


class C120Error(RuntimeError):
    pass


def wrap01(value: float) -> float:
    return value - math.floor(value)


def hue_degrees(led_index: int, led_count: int, phase: float) -> float:
    """Hue at one LED. Phase 0 and 1 are the same colour.

    Positive time moves a colour toward higher LED indexes. Index 0 is the
    LED nearest the controller on the string this was written for.
    """
    if led_count <= 0:
        raise ValueError("led_count must be positive")
    turns = RAINBOW_COUNT * led_index / led_count - phase
    return wrap01(turns) * 360.0


def _byte(channel: float) -> int:
    if channel <= 0:
        return 0
    if channel >= 1:
        return 255
    return int(math.floor(channel * 255.0 + 0.5))


def led_wrgb(hue: float) -> tuple[int, int, int, int]:
    """White, red, green, blue. White stays 0 so the rainbow is not washed out."""
    red, green, blue = colorsys.hsv_to_rgb(wrap01(hue / 360.0), 1.0, 1.0)
    return (0, _byte(red), _byte(green), _byte(blue))


def render_frame(led_count: int, phase: float) -> bytes:
    parts = bytearray()
    for index in range(led_count):
        parts.extend(led_wrgb(hue_degrees(index, led_count, phase)))
    return bytes(parts)


def encode_movie(led_count: int) -> bytes:
    """Frames ``0 .. FRAMES-1`` at phases ``k / FRAMES``.

    Phase 1 is not stored. The next loop is frame 0, one step after the last.
    """
    if FRAMES < 2:
        raise ValueError("a looping movie needs at least 2 frames")
    return b"".join(render_frame(led_count, index / FRAMES) for index in range(FRAMES))


def plan_text() -> str:
    return (
        f"{MOVIE_NAME}: {FRAMES} frames, {FPS} fps, {FRAME_DELAY_MS} ms frame delay\n"
        f"{RAINBOW_COUNT} rainbows, full HSV, white channel 0, dimmer {BRIGHTNESS_PERCENT}%\n"
        f"about {REVOLUTION_SECONDS} seconds per revolution if the string plays at {FPS} fps"
    )


class Client:
    def __init__(self, host: str, port: int = 80, timeout: float = 8.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.token: str | None = None

    def gestalt(self) -> dict:
        body = self._request("GET", "/xled/v1/gestalt", None, auth=False)
        if not isinstance(body, dict):
            raise C120Error("gestalt was not an object")
        return body

    def firmware(self) -> str:
        body = self._request("GET", "/xled/v1/fw/version", None, auth=False)
        version = body.get("version")
        if not isinstance(version, str):
            raise C120Error(f"firmware version missing: {body}")
        return version

    def login(self) -> None:
        challenge = base64.b64encode(os.urandom(32)).decode("ascii")
        body = self._request("POST", "/xled/v1/login", {"challenge": challenge}, auth=False)
        token = body.get("authentication_token")
        if not isinstance(token, str):
            raise C120Error("login response did not include authentication_token")
        raw = base64.b64decode(token)
        if len(raw) != 8:
            raise C120Error(f"authentication token decoded to {len(raw)} bytes, expected 8")
        self.token = token
        try:
            # A rejected verify must fail this login. Retrying it would log in again.
            verified = self._request(
                "POST",
                "/xled/v1/verify",
                {"challenge-response": body.get("challenge-response")},
                auth=True,
                retry=False,
            )
        except Exception:
            self.token = None
            raise
        if verified.get("code") not in (None, 1000):
            self.token = None
            raise C120Error(f"verify failed: {verified}")

    def movies(self) -> dict:
        body = self._request("GET", "/xled/v1/movies", None, auth=True)
        if not isinstance(body, dict) or not isinstance(body.get("movies"), list):
            raise C120Error(f"movie list was not usable: {body}")
        return body

    def mode(self) -> str:
        body = self._request("GET", "/xled/v1/led/mode", None, auth=True)
        mode = body.get("mode")
        if not isinstance(mode, str):
            raise C120Error(f"mode missing from {body}")
        return mode

    def upload(self, movie: bytes, leds: int) -> dict:
        expected = FRAMES * leds * 4
        if len(movie) != expected:
            raise C120Error(f"movie is {len(movie)} bytes, expected {expected}")
        # No retry on these posts. A retried movies/new would create a second slot,
        # and this program will not delete it.
        created = self._request(
            "POST",
            "/xled/v1/movies/new",
            {
                "descriptor_type": "rgbw_raw",
                "fps": FPS,
                "frames_number": FRAMES,
                "leds_per_frame": leds,
                "name": MOVIE_NAME,
                "unique_id": str(uuid.uuid4()),
            },
            auth=True,
            retry=False,
        )
        try:
            uploaded = self._request(
                "POST",
                "/xled/v1/movies/full",
                movie,
                auth=True,
                octet=True,
                retry=False,
                timeout=UPLOAD_TIMEOUT,
            )
        except C120Error as exc:
            raise C120Error(
                f"{exc}. A movie slot may now exist. This program will not delete it. "
                "Check the Twinkly app before trying again."
            ) from exc
        return {"created": created, "uploaded": uploaded}

    def configure_and_play(self, leds: int) -> None:
        self._request(
            "POST",
            "/xled/v1/led/movie/config",
            {"frame_delay": FRAME_DELAY_MS, "leds_number": leds, "frames_number": FRAMES},
            auth=True,
            retry=False,
        )
        self._request("POST", "/xled/v1/led/mode", {"mode": "movie"}, auth=True, retry=False)
        self._request(
            "POST",
            "/xled/v1/led/out/brightness",
            {"mode": "enabled", "type": "A", "value": BRIGHTNESS_PERCENT},
            auth=True,
            retry=False,
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | bytes | None,
        auth: bool,
        octet: bool = False,
        retry: bool = True,
        timeout: float | None = None,
    ) -> dict:
        if auth and not self.token:
            raise C120Error(f"{method} {path} called before login")
        headers = {"Accept": "application/json"}
        data: bytes | None
        if isinstance(payload, bytes):
            data = payload
            headers["Content-Type"] = "application/octet-stream" if octet else "application/json"
        elif payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            data = None
        if auth and self.token:
            headers["X-Auth-Token"] = self.token
        request = urllib.request.Request(
            f"http://{self.host}:{self.port}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as response:
                raw = response.read(JSON_LIMIT + 1)
                status = response.status
        except urllib.error.HTTPError as err:
            detail = err.read(4096).decode("utf-8", "replace")
            err.close()
            if err.code == 401 and auth and retry:
                self.token = None
                self.login()
                return self._request(method, path, payload, auth=True, octet=octet, retry=False, timeout=timeout)
            raise C120Error(f"{method} {path} HTTP {err.code}: {detail}") from err
        except TimeoutError as err:
            raise C120Error(f"{method} {path} timed out") from err
        except urllib.error.URLError as err:
            raise C120Error(f"{method} {path} failed: {err.reason}") from err
        if len(raw) > JSON_LIMIT:
            raise C120Error(f"{method} {path} response was larger than {JSON_LIMIT} bytes")
        if status == 204 or not raw:
            return {}
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as err:
            raise C120Error(f"{method} {path} returned non-JSON ({len(raw)} bytes)") from err
        if not isinstance(body, dict):
            raise C120Error(f"{method} {path} returned {type(body).__name__}")
        if body.get("code") not in (None, 1000):
            raise C120Error(f"{method} {path} application code {body.get('code')}: {body}")
        return body


def check_host(host: str) -> str:
    if not host or len(host) > 253:
        raise C120Error("pass the string's IP address or hostname")
    labels = host.split(".")
    for label in labels:
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            raise C120Error(f"not a hostname or IPv4 address: {host}")
        if not all(character.isalnum() or character == "-" for character in label):
            raise C120Error(f"not a hostname or IPv4 address: {host}")
    return host


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise C120Error(f"{label} was missing or not an integer")
    return value


def screen_gestalt(gestalt: dict) -> int:
    """Return the LED count, or raise before any login or write."""
    profile = gestalt.get("led_profile")
    bytes_per_led = _as_int(gestalt.get("bytes_per_led"), "bytes_per_led")
    if profile != "RGBW" or bytes_per_led != 4:
        raise C120Error(
            f"this script sends RGBW frames. The string reported {profile}, {bytes_per_led} bytes per LED"
        )
    leds = _as_int(gestalt.get("number_of_led"), "number_of_led")
    if not 1 <= leds <= MAX_LEDS:
        raise C120Error(f"number_of_led {leds} is outside 1..{MAX_LEDS}")
    supported = gestalt.get("max_supported_led")
    if isinstance(supported, int) and not isinstance(supported, bool) and leds > supported:
        raise C120Error(f"number_of_led {leds} is above max_supported_led {supported}")
    capacity = _as_int(gestalt.get("movie_capacity"), "movie_capacity")
    if capacity < FRAMES:
        raise C120Error(f"movie capacity is {capacity} frames. C.120 needs {FRAMES}")
    compatibility = gestalt.get("compatibility_mode")
    if isinstance(compatibility, str) and compatibility.lower() in {"disabled", "off", "false"}:
        raise C120Error("Compatibility Mode is off. Turn it on in the Twinkly app, then try again")
    if compatibility == 0:
        raise C120Error("Compatibility Mode is off. Turn it on in the Twinkly app, then try again")
    return leds


def screen_movies(listing: dict, leds: int) -> None:
    existing = listing["movies"]
    if existing:
        count = len(existing)
        noun = "movie" if count == 1 else "movies"
        pronoun = "it" if count == 1 else "them"
        raise C120Error(
            f"the string already stores {count} {noun}. "
            f"Nothing was written. Delete {pronoun} in the Twinkly app first. This script will not delete {pronoun}"
        )
    available = listing.get("available_frames")
    if isinstance(available, int) and not isinstance(available, bool) and available < FRAMES:
        raise C120Error(f"available_frames is {available}. C.120 needs {FRAMES}")
    if leds * FRAMES * 4 <= 0:
        raise C120Error("refusing an empty movie")


def movie_ok(movie: dict, leds: int) -> bool:
    frames = movie.get("frames_number", FRAMES)
    count = movie.get("leds_per_frame", leds)
    kind = movie.get("descriptor_type", "rgbw_raw")
    return frames == FRAMES and count == leds and kind == "rgbw_raw"


def describe(gestalt: dict, firmware: str, leds: int, listing: dict | None, mode: str | None) -> str:
    stored = len(listing["movies"]) if listing else "not read"
    available = listing.get("available_frames") if listing else None
    lines = [
        (
            f"{gestalt.get('product_code', 'Twinkly')}, firmware {firmware}, "
            f"hardware {gestalt.get('hardware_version', 'unknown')}"
        ),
        (
            f"{leds} RGBW LEDs, capacity {gestalt.get('movie_capacity')} frames, "
            f"compatibility mode {gestalt.get('compatibility_mode', 'not reported')}"
        ),
        f"movies stored: {stored}",
    ]
    if available is not None:
        lines[-1] += f", available frames: {available}"
    if mode:
        lines.append(f"mode: {mode}")
    return "\n".join(lines)


def read_device(client: Client) -> tuple[dict, str, int, dict, str]:
    gestalt = client.gestalt()
    firmware = client.firmware()
    leds = screen_gestalt(gestalt)
    # Login invalidates any other client's token. Do it only after the string
    # looks like a device this movie can fit.
    client.login()
    listing = client.movies()
    mode = client.mode()
    return gestalt, firmware, leds, listing, mode


def run_plan() -> int:
    print(plan_text())
    return 0


def run_inspect(client: Client) -> int:
    gestalt, firmware, leds, listing, mode = read_device(client)
    print(describe(gestalt, firmware, leds, listing, mode))
    print(plan_text())
    if listing["movies"]:
        print("upload will refuse while a movie is stored. This script will not delete it.")
    return 0


def run_upload(client: Client, yes: bool) -> int:
    gestalt, firmware, leds, listing, mode = read_device(client)
    print(describe(gestalt, firmware, leds, listing, mode))
    print(plan_text())
    screen_movies(listing, leds)
    if not yes:
        print("Nothing was written. Re-run with --yes to upload and play it.")
        return 0
    movie = encode_movie(leds)
    client.upload(movie, leds)
    listing = client.movies()
    stored = listing["movies"]
    if len(stored) != 1 or not movie_ok(stored[0], leds):
        raise C120Error(
            "the stored movie did not match C.120, so playback was left unchanged. "
            "This program will not delete the slot. Check the Twinkly app"
        )
    client.configure_and_play(leds)
    stored_fps = stored[0].get("fps")
    fps_note = f" The controller stored fps as {stored_fps}." if stored_fps not in (None, FPS) else ""
    print(
        f"Uploaded {MOVIE_NAME} and set the string to play it at {BRIGHTNESS_PERCENT}% brightness. "
        f"{FRAMES} frames, {leds} RGBW LEDs, frame delay {FRAME_DELAY_MS} ms.{fps_note}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Upload the C.120 rainbow movie to a Twinkly RGBW string. Writes only with --yes, and never deletes."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan", help="print the movie. Does not use the network")
    inspect = sub.add_parser("inspect", help="read the string. Logs in, does not change playback")
    inspect.add_argument("host")
    inspect.add_argument("--port", type=int, default=80)
    upload = sub.add_parser("upload", help="read the string, and upload only when --yes is passed")
    upload.add_argument("host")
    upload.add_argument("--port", type=int, default=80)
    upload.add_argument("--yes", action="store_true", help="upload the movie and play it")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return run_plan()
        if not 1 <= args.port <= 65535:
            raise C120Error("--port must be 1..65535")
        client = Client(check_host(args.host), args.port)
        if args.command == "inspect":
            return run_inspect(client)
        return run_upload(client, args.yes)
    except C120Error as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
