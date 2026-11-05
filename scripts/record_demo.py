"""Record the README demo GIF from the real web UI (real model call, no mock-up).

Requirements (dev only): `pip install -e ".[demo]"` and Google Chrome installed.
Start the server first, then run:

    EMPORIO_TODAY=2026-03-25 python -m emporio.web --port 8765
    python scripts/record_demo.py --url http://127.0.0.1:8765 --out docs/assets/demo.gif

Frames are captured with Playwright while a question is typed and answered. The wait
for the model is shortened in the GIF (only the first ~1.5 s of "thinking" is kept),
everything else plays at real speed.
"""

from __future__ import annotations

import argparse
import io
import time
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

VIEWPORT = {"width": 1280, "height": 760}
OUT_WIDTH = 880
FPS = 10


def grab(page, frames: list[Image.Image]) -> None:
    png = page.screenshot(type="png")
    img = Image.open(io.BytesIO(png)).convert("RGB")
    frames.append(img.resize((OUT_WIDTH, round(img.height * OUT_WIDTH / img.width)), Image.LANCZOS))


def record_for(page, frames, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        t0 = time.monotonic()
        grab(page, frames)
        time.sleep(max(0, 1 / FPS - (time.monotonic() - t0)))


def record_until(page, frames, js_condition: str, timeout_s: float = 90, max_frames: int | None = None) -> None:
    end = time.monotonic() + timeout_s
    taken = 0
    while time.monotonic() < end and not page.evaluate(js_condition):
        if max_frames is None or taken < max_frames:
            t0 = time.monotonic()
            grab(page, frames)
            taken += 1
            time.sleep(max(0, 1 / FPS - (time.monotonic() - t0)))
        else:
            time.sleep(0.2)  # keep waiting for the model without recording (shortened wait)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--out", default="docs/assets/demo.gif")
    parser.add_argument("--question", default="Quanto custa o Takamine GD20?")
    parser.add_argument("--screenshot", default="docs/assets/ui.png")
    args = parser.parse_args()

    frames: list[Image.Image] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
        page.goto(args.url)
        page.wait_for_load_state("networkidle")
        page.mouse.move(900, 300)
        record_for(page, frames, 3.2)  # greeting typed + idle blink
        page.click("#input")
        for ch in args.question:  # human-like typing
            page.keyboard.type(ch)
            if len(frames) % 2 == 0:
                grab(page, frames)
            time.sleep(0.03)
        record_for(page, frames, 0.4)
        page.keyboard.press("Enter")
        talking = "document.querySelector('#heroOrb').dataset.state === 'talking'"
        idle = "document.querySelector('#heroOrb').dataset.state === 'idle'"
        record_until(page, frames, talking, max_frames=int(1.5 * FPS))
        record_until(page, frames, idle, timeout_s=30)
        page.mouse.move(300, 420)
        record_for(page, frames, 2.5)
        Path(args.screenshot).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=args.screenshot)
        browser.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    palette_src = frames[-1].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    gif_frames = [f.quantize(palette=palette_src, dither=Image.Dither.NONE) for f in frames]
    gif_frames[0].save(
        out, save_all=True, append_images=gif_frames[1:], duration=int(1000 / FPS), loop=0, optimize=True, disposal=1
    )
    print(f"{len(frames)} frames → {out} ({out.stat().st_size / 1e6:.1f} MB); screenshot → {args.screenshot}")


if __name__ == "__main__":
    main()
