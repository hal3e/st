#!/usr/bin/env python3
"""Exercise the real terminal and its X11 output; requires Xvfb and libX11."""

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tty
import unittest
import zlib


def client(path):
    tty.setraw(0)
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(path)
        stream = connection.makefile("rwb", buffering=0)
        stream.write(json.dumps({"window": int(os.environ["WINDOWID"])}).encode() + b"\n")
        pending = b""
        for line in stream:
            request = json.loads(line)
            output = request["write"].encode("utf-8")
            offset = 0
            while offset < len(output):
                offset += os.write(1, output[offset:offset + 4096])
            deadline = time.monotonic() + 5
            while b"\x1b[0n" not in pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([0], [], [], remaining)[0]:
                    raise RuntimeError("terminal did not answer the synchronization query")
                pending += os.read(0, 65536)
            end = pending.index(b"\x1b[0n") + 4
            response, pending = pending[:end], pending[end:]
            stream.write(json.dumps({"reply": base64.b64encode(response).decode()}).encode() + b"\n")


def command(keys, payload=b""):
    return "\x1b_G" + keys + ";" + base64.b64encode(payload).decode() + "\x1b\\"


def png(color, width=16, height=16):
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data))

    pixels = b"".join(b"\0" + bytes(color) * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


class XKeyEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
                ("state", ctypes.c_uint), ("keycode", ctypes.c_uint),
                ("same_screen", ctypes.c_int)]


class XEvent(ctypes.Union):
    _fields_ = [("key", XKeyEvent), ("padding", ctypes.c_long * 24)]


class GraphicsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("Xvfb"):
            raise RuntimeError("graphics tests require Xvfb")
        cls.display_temp = tempfile.TemporaryDirectory(prefix="st-graphics-display-")
        read_fd, write_fd = os.pipe()
        cls.xvfb_log = open(Path(cls.display_temp.name) / "xvfb.log", "w+")
        cls.xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1024x768x24", "-nolisten", "tcp"],
            pass_fds=[write_fd], stdout=cls.xvfb_log, stderr=cls.xvfb_log)
        os.close(write_fd)
        if not select.select([read_fd], [], [], 10)[0]:
            cls.xvfb.terminate()
            raise RuntimeError("Xvfb did not start")
        cls.display_name = ":" + os.read(read_fd, 64).decode().strip()
        os.close(read_fd)
        cls.x = ctypes.CDLL("libX11.so.6")
        signatures = {
            "XOpenDisplay": ([ctypes.c_char_p], ctypes.c_void_p),
            "XCloseDisplay": ([ctypes.c_void_p], ctypes.c_int),
            "XDefaultRootWindow": ([ctypes.c_void_p], ctypes.c_ulong),
            "XGetGeometry": ([ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong)]
                             + [ctypes.POINTER(ctypes.c_int)] * 2
                             + [ctypes.POINTER(ctypes.c_uint)] * 4, ctypes.c_int),
            "XGetImage": ([ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                           ctypes.c_uint, ctypes.c_uint, ctypes.c_ulong, ctypes.c_int], ctypes.c_void_p),
            "XGetPixel": ([ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_ulong),
            "XDestroyImage": ([ctypes.c_void_p], ctypes.c_int),
            "XKeysymToKeycode": ([ctypes.c_void_p, ctypes.c_ulong], ctypes.c_uint),
            "XSendEvent": ([ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int, ctypes.c_long,
                            ctypes.POINTER(XEvent)], ctypes.c_int),
            "XFlush": ([ctypes.c_void_p], ctypes.c_int),
            "XResizeWindow": ([ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_uint], ctypes.c_int),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(cls.x, name)
            function.argtypes, function.restype = arguments, result
        cls.display = cls.x.XOpenDisplay(cls.display_name.encode())
        if not cls.display:
            raise RuntimeError("could not connect to Xvfb")

    @classmethod
    def tearDownClass(cls):
        cls.x.XCloseDisplay(cls.display)
        cls.xvfb.terminate()
        cls.xvfb.wait(timeout=5)
        cls.xvfb_log.close()
        cls.display_temp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="st-graphics-test-")
        self.addCleanup(self.temp.cleanup)
        self.stderr = open(Path(self.temp.name) / "stderr", "w+")
        self.addCleanup(self.stderr.close)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.settimeout(10)
        self.addCleanup(self.server.close)
        path = str(Path(self.temp.name) / "control")
        self.server.bind(path)
        self.server.listen(1)
        helpers = Path(self.temp.name) / "bin"
        helpers.mkdir()
        copyout = helpers / "st-copyout"
        copyout.write_text('#!/bin/sh\ncat > "$ST_TEST_COPYOUT"\n')
        copyout.chmod(0o755)
        self.copyout = Path(self.temp.name) / "copyout"
        environment = dict(os.environ, DISPLAY=self.display_name, LC_ALL="C.UTF-8",
                           PATH=str(helpers) + os.pathsep + os.environ["PATH"],
                           ST_TEST_COPYOUT=str(self.copyout))
        self.process = subprocess.Popen(
            [ST_BINARY, "-g", "40x12", "-f", "DejaVu Sans Mono:pixelsize=14",
             "-e", sys.executable, str(Path(__file__).resolve()), "--client", path],
            env=environment, stdout=subprocess.DEVNULL, stderr=self.stderr)
        self.addCleanup(self.stop_terminal)
        try:
            self.connection, _ = self.server.accept()
        except TimeoutError:
            self.stderr.seek(0)
            self.fail("terminal did not start: " + self.stderr.read())
        self.connection.settimeout(8)
        self.addCleanup(self.connection.close)
        self.stream = self.connection.makefile("rwb", buffering=0)
        self.addCleanup(self.stream.close)
        self.window = json.loads(self.stream.readline())["window"]
        response = self.send("\x1b[?25l\x1b[16t")
        dimensions = re.search(rb"\x1b\[6;(\d+);(\d+)t", response)
        self.assertIsNotNone(dimensions, response)
        self.ch, self.cw = map(int, dimensions.groups())
        root = ctypes.c_ulong()
        x, y = ctypes.c_int(), ctypes.c_int()
        width, height, border, depth = [ctypes.c_uint() for _ in range(4)]
        self.x.XGetGeometry(self.display, self.window, ctypes.byref(root), ctypes.byref(x),
                            ctypes.byref(y), ctypes.byref(width), ctypes.byref(height),
                            ctypes.byref(border), ctypes.byref(depth))
        self.width, self.height = width.value, height.value
        self.left, self.top = (self.width - 40 * self.cw) // 2, (self.height - 12 * self.ch) // 2

    def stop_terminal(self):
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)

            self.fail("terminal did not exit after its application closed")
        self.stderr.seek(0)
        diagnostics = self.stderr.read()
        self.assertEqual(self.process.returncode, 0, diagnostics)
        self.assertNotIn("AddressSanitizer", diagnostics)
        self.assertNotIn("runtime error:", diagnostics)

    def send(self, output):
        self.stream.write(json.dumps({"write": output + "\x1b[5n"}).encode() + b"\n")
        line = self.stream.readline()
        if not line:
            self.stderr.seek(0)
            self.fail("terminal exited: " + self.stderr.read())
        return base64.b64decode(json.loads(line)["reply"])

    def samples(self, cells):
        image = self.x.XGetImage(self.display, self.window, 0, 0,
                                self.width, self.height, ctypes.c_ulong(-1), 2)
        self.assertTrue(image)
        try:
            colors = []
            for col, row in cells:
                pixel = self.x.XGetPixel(image, self.left + col * self.cw + self.cw // 2,
                                        self.top + row * self.ch + self.ch // 2)
                colors.append(((pixel >> 16) & 255, (pixel >> 8) & 255, pixel & 255))
            return colors
        finally:
            self.x.XDestroyImage(image)

    def assert_color(self, color, cells=((0, 0), (1, 0), (0, 1), (1, 1))):
        deadline = time.monotonic() + 3
        while True:
            actual = self.samples(cells)
            if all(all(abs(a - b) <= 2 for a, b in zip(pixel, color)) for pixel in actual):
                return
            if time.monotonic() > deadline:
                self.fail(f"expected {color} at {cells}, found {actual}")
            time.sleep(0.02)

    def place(self, image_id=11, color=(255, 0, 0), **options):
        keys = {"a": "T", "f": 24, "i": image_id, "p": 1, "s": 16, "v": 16,
                "c": 2, "r": 2, "C": 1, **options}
        return self.send(command(",".join(f"{key}={value}" for key, value in keys.items()),
                                 bytes(color) * 256))

    def key(self, keysym, state=0):
        event = XEvent()
        event.key.type = 2
        event.key.display = self.display
        event.key.window = self.window
        event.key.root = self.x.XDefaultRootWindow(self.display)
        event.key.state = state
        event.key.keycode = self.x.XKeysymToKeycode(self.display, keysym)
        event.key.same_screen = 1
        self.x.XSendEvent(self.display, self.window, 0, 1, ctypes.byref(event))
        self.x.XFlush(self.display)

    def test_query_reports_support_without_keeping_the_image(self):
        # Given a valid one-pixel capability probe.
        probe = command("a=q,i=31,f=24,s=1,v=1", b"\0\xff\0")
        # When the application queries support, then the response succeeds.
        self.assertIn(b"i=31;OK", self.send(probe))
        # Then a query has not installed an image that can be placed.
        self.assertIn(b"ENOENT", self.send(command("a=p,i=31,p=1")))

    def test_raw_images_render_and_preserve_the_cursor(self):
        # Given an empty terminal, when a raw image is placed with C=1.
        self.assertIn(b"OK", self.place())
        # Then pixels render and the cursor remains at its original location.
        self.assert_color((255, 0, 0))
        self.assertIn(b"\x1b[1;1R", self.send("\x1b[6n"))
        self.send(command("a=d,d=I,i=11"))
        # Given a cursor near the bottom, a clipped placement also preserves it.
        self.send("\x1b[12;1H")
        self.place(image_id=12, r=4)
        self.assertIn(b"\x1b[12;1R", self.send("\x1b[6n"))

    def test_png_rgba_and_compressed_uploads_render(self):
        # Given each supported data format, when it is transmitted and displayed.
        cases = [("f=100", png((0, 255, 0)), (0, 255, 0)),
                 ("f=32,s=16,v=16", bytes((0, 0, 255, 255)) * 256, (0, 0, 255)),
                 ("f=24,s=16,v=16,o=z", zlib.compress(bytes((255, 0, 255)) * 256), (255, 0, 255))]
        for index, (format_keys, data, color) in enumerate(cases):
            with self.subTest(format=format_keys):
                response = self.send(command(f"a=T,i={20 + index},p=1,c=2,r=2,C=1,{format_keys}", data))
                # Then the acknowledgement and rendered color match the image.
                self.assertIn(b"OK", response)
                self.assert_color(color)

    def test_chunked_uploads_only_display_after_the_final_chunk(self):
        # Given a split RGB image, when only the first chunk has arrived.
        data = bytes((0, 255, 255)) * 256
        response = self.send(command("a=T,i=30,p=1,f=24,s=16,v=16,c=2,r=2,C=1,m=1", data[:384]))
        self.assertNotIn(b"OK", response)
        # Then the final chunk completes both the transfer and placement.
        self.assertIn(b"OK", self.send(command("m=0", data[384:])))
        self.assert_color((0, 255, 255))

    def test_rgba_transparency_preserves_the_background(self):
        # Given opaque text-cell backgrounds and fully transparent image pixels.
        self.send("\x1b[48;2;10;20;200m  \r\n  \x1b[0m\x1b[H")
        # When the image is composited, then the backgrounds remain visible.
        self.assertIn(b"OK", self.send(command(
            "a=T,f=32,i=35,p=1,s=1,v=1,c=2,r=2,C=1", bytes((255, 0, 0, 0)))))
        self.assert_color((10, 20, 200))

        # Then partially transparent pixels blend with those same backgrounds.
        self.assertIn(b"OK", self.send(command(
            "a=T,f=32,i=36,p=1,s=1,v=1,c=2,r=2,C=1", bytes((255, 0, 0, 128)))))
        self.assert_color((133, 10, 100))

    def test_cached_images_can_be_placed_again_after_unloading(self):
        # Given an uploaded image whose rendered data has been unloaded.
        self.place()
        self.assert_color((255, 0, 0))
        self.key(0xFFC4, 5)  # Ctrl-Shift-F7.
        # When its cached identifier is placed elsewhere without another upload.
        self.assertIn(b"OK", self.send("\x1b[5;1H" + command("a=p,i=11,p=2,c=2,r=2,C=1")))
        # Then the new placement reloads from the cache and displays the image.
        self.assert_color((255, 0, 0), ((0, 4), (1, 4), (0, 5), (1, 5)))

    def test_resizing_retains_images_and_updates_pixel_queries(self):
        # Given a displayed image, when the window grows by complete cells.
        self.place()
        self.assert_color((255, 0, 0))
        self.x.XResizeWindow(self.display, self.window,
                             self.width + self.cw * 5, self.height + self.ch * 2)
        self.x.XFlush(self.display)
        deadline = time.monotonic() + 3
        while b"\x1b[8;14;45t" not in self.send("\x1b[18t"):
            self.assertLess(time.monotonic(), deadline, "terminal did not resize")
            time.sleep(0.02)
        # Then both the image and client-visible dimensions remain correct.
        self.assert_color((255, 0, 0))
        reply = self.send("\x1b[14t")
        self.assertIn(f"\x1b[4;{14 * self.ch};{45 * self.cw}t".encode(), reply)

    def test_files_and_shared_memory_render_without_removing_regular_files(self):
        # Given local pixel data, when it is transferred by a regular filename.
        path = Path(self.temp.name) / "image.rgb"
        path.write_bytes(bytes((255, 255, 0)) * 256)
        self.assertIn(b"OK", self.send(command("a=T,t=f,i=40,p=1,f=24,s=16,v=16,c=2,r=2,C=1", os.fsencode(path))))
        # Then the image renders and the source file remains intact.
        self.assert_color((255, 255, 0))
        self.assertTrue(path.exists())
        # Given a shared-memory object, the terminal consumes and unlinks it.
        name = f"st-graphics-test-{os.getpid()}"
        shared = Path("/dev/shm") / name
        self.addCleanup(lambda: shared.unlink(missing_ok=True))
        shared.write_bytes(b"padding" + bytes((255, 128, 0)) * 256)
        self.assertIn(b"OK", self.send(command("a=T,t=s,i=41,p=1,f=24,s=16,v=16,S=768,O=7,c=2,r=2,C=1", ("/" + name).encode())))
        self.assert_color((255, 128, 0))
        self.assertFalse(shared.exists())

    def test_unicode_placeholders_use_foreground_and_underline_ids(self):
        # Given a virtual placement with distinct image and placement identifiers.
        width, height = self.cw * 2, self.ch * 2
        self.assertIn(b"OK", self.send(command(
            f"a=T,f=24,i=1193046,p=11259375,s={width},v={height},c=2,r=2,C=1,U=1",
            bytes((200, 50, 100)) * width * height)))
        # When cells select those identifiers and provide row/column diacritics.
        prefix = "\x1b[38;2;18;52;86;58;2;171;205;239m"
        placeholder = chr(0x10EEEE)
        row = lambda row_mark: "".join(placeholder + row_mark + col for col in ("\u0305", "\u030d"))
        self.send(prefix + row("\u0305") + "\r\n" + row("\u030d") + "\x1b[0m")
        # Then the image renders instead of sending its packed cells to HarfBuzz.
        self.assert_color((200, 50, 100))

    def test_temporary_file_queries_are_safe_and_quiet_uploads_stay_quiet(self):
        # Given a temporary image, querying it may delete the query's image state.
        path = Path(self.temp.name) / "tty-graphics-protocol-query.png"
        path.write_bytes(png((0, 255, 0)))
        # When the temporary-file query completes, then its reply is valid.
        self.assertIn(b"OK", self.send(command("a=q,t=t,i=45,f=100", os.fsencode(path))))
        self.assertFalse(path.exists())
        # Given an explicitly quiet upload with a full 32-bit image identifier.
        response = self.place(image_id=0xFEDCBA98, q=2)
        # Then no graphics reply leaks through, while the image still displays.
        self.assertNotIn(b"\x1b_G", response)
        self.assert_color((255, 0, 0))

    def test_deleting_an_image_restores_the_text_background(self):
        # Given text with a true-color background underneath a classic placement.
        self.send("\x1b[48;2;10;20;200m  \r\n  \x1b[0m\x1b[H")
        self.place()
        self.assert_color((255, 0, 0))
        # When the image is deleted, then the original cell backgrounds return.
        self.send(command("a=d,d=I,i=11"))
        self.assert_color((10, 20, 200))

    def test_images_survive_alternate_screen_switches(self):
        # Given an image in the primary screen.
        self.place()
        self.assert_color((255, 0, 0))
        # When an alternate-screen application places a different image.
        self.send("\x1b[?1049h\x1b[H")
        self.place(image_id=12, color=(0, 255, 0))
        self.assert_color((0, 255, 0))
        # Then leaving that application restores the primary image.
        self.send("\x1b[?1049l")
        self.assert_color((255, 0, 0))

    def test_copyout_exports_text_and_image_placeholders(self):
        # Given ordinary text around an image with packed cell metadata.
        self.send("BEFORE\x1b[3;1HAFTER\x1b[2;1H")
        self.place(c=1, r=1)
        # When copyout is invoked using either the default or personal binding.
        self.key(ord("o"), 8)  # Alt-o.
        self.key(ord("o"), 12)  # Ctrl-Alt-o.
        deadline = time.monotonic() + 3
        while not self.copyout.exists() or b"AFTER" not in self.copyout.read_bytes():
            self.assertLess(time.monotonic(), deadline, "copyout did not finish")
            time.sleep(0.02)
        # Then text survives and image metadata is exported as valid Unicode.
        output = self.copyout.read_bytes().decode("utf-8", errors="strict")
        self.assertIn("BEFORE", output)
        self.assertIn("AFTER", output)
        self.assertIn(chr(0x10EEEE), output)
        self.assertNotIn("\0", output)

    def test_scrollback_images_can_be_deleted_without_resetting_the_view(self):
        # Given an image that has scrolled into history.
        self.send("\x1b[3;1H\x1b[48;2;11;22;33m  \x1b[0m\x1b[H")
        self.place()
        self.send("\x1b[12;1H" + "\r\n" * 12)
        self.key(0xFF55, 1)  # Shift-PageUp.
        self.key(0xFF55, 1)
        self.assert_color((255, 0, 0))
        self.assert_color((11, 22, 33), ((0, 2), (1, 2)))
        # When the application deletes it while history is displayed.
        self.send(command("a=d,d=I,i=11"))
        # Then a protocol reply does not force a return to the live screen.
        self.assert_color((11, 22, 33), ((0, 2), (1, 2)))
        # Reusing the identifier must not resurrect old placements in history.
        self.place(color=(0, 255, 0), U=1)
        self.assert_color((11, 22, 33), ((0, 2), (1, 2)))
        self.assertNotEqual(self.samples(((0, 0),))[0], (0, 255, 0))

    def test_invalid_transfers_report_errors_and_leave_the_terminal_responsive(self):
        # Given malformed or oversized application requests.
        cases = [command("a=q,i=80,f=24,s=2147483647,v=2147483647", b"x"),
                 command("a=q,i=81,f=24,s=0,v=1", b"x"),
                 command("a=q,i=82,f=24,s=2,v=2", b"x"),
                 command("a=T,i=83,f=24,s=1,v=1,c=512,r=1", b"rgb"),
                 command("a=q,i=84,f=24,s=-1,v=1", b"x"),
                 command("a=q,i=85,t=f,f=100", b"/dev/zero"),
                 command("a=T,i=88,f=24,s=2,v=2,c=2,r=2", b"x"),
                 command("a=T,i=89,f=100,c=2,r=2", b"invalid PNG"),
                 command("a=T,i=90,f=24,s=1,v=1,c=0,r=511", b"rgb"),
                 "\x1b_Ga=q,i=91,f=24,s=1,v=1;!!!!\x1b\\",
                 command("a=q,t=f,i=92,f=100", b"/tmp/image\0ignored")]
        for sequence in cases:
            with self.subTest(sequence=sequence):
                # When rejected, then the command never returns false success.
                response = self.send(sequence)
                self.assertRegex(response, rb"E(?:INVAL|FBIG|BADF)")
                self.assertNotIn(b";OK", response)
        # Given a truncated shared-memory object, reject it before mmap access.
        name = f"st-graphics-truncated-{os.getpid()}"
        path = Path("/dev/shm") / name
        path.write_bytes(b"x")
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self.assertIn(b"EINVAL", self.send(command("a=q,i=86,t=s,f=24,s=2,v=2", ("/" + name).encode())))
        # Then a subsequent valid request still succeeds.
        self.assertIn(b"OK", self.place(image_id=87))
        self.assert_color((255, 0, 0))

    def test_sgr_subparameters_do_not_consume_following_attributes(self):
        # Given underline styles followed by independent background attributes.
        for style in ("4", "4:0", "4:3"):
            # When a combined SGR command is parsed, the background still applies.
            self.send("\x1b[H\x1b[0m\x1b[" + style + ";48:2::10:20:200m  \r\n  \x1b[0m")
            # Then the displayed cells retain the requested RGB background.
            self.assert_color((10, 20, 200))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--client")
    parser.add_argument("--st", default="./st")
    options, remaining = parser.parse_known_args()
    if options.client:
        client(options.client)
    else:
        ST_BINARY = str(Path(options.st).resolve())
        unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
