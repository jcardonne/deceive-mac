"""Render the app icon with AppKit so the repo carries no binary assets."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSGraphicsContext,
    NSMakeRect,
)

ROOT = Path(__file__).resolve().parent.parent
ICONSET = ROOT / "assets" / "AppIcon.iconset"
ICNS = ROOT / "assets" / "AppIcon.icns"

# name -> pixel size, exactly the set iconutil expects
ENTRIES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def render(size: int) -> bytes:
    rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, size, size, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0,
    )
    context = NSGraphicsContext.graphicsContextWithBitmapImageRep_(rep)
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.setCurrentContext_(context)

    inset = size * 0.055
    plate = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(inset, inset, size - 2 * inset, size - 2 * inset),
        size * 0.22, size * 0.22,
    )
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.09, 0.11, 0.15, 1.0).set()
    plate.fill()

    circle = NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(size * 0.26, size * 0.26, size * 0.48, size * 0.48),
    )
    circle.setLineWidth_(size * 0.058)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.93, 0.96, 1.0, 1.0).set()
    circle.stroke()

    NSBezierPath.bezierPathWithOvalInRect_(
        NSMakeRect(size * 0.47, size * 0.47, size * 0.06, size * 0.06),
    ).fill()

    slash = NSBezierPath.bezierPath()
    slash.moveToPoint_((size * 0.29, size * 0.29))
    slash.lineToPoint_((size * 0.71, size * 0.71))
    slash.setLineWidth_(size * 0.075)
    slash.setLineCapStyle_(1)
    NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.45, 0.38, 1.0).set()
    slash.stroke()

    NSGraphicsContext.restoreGraphicsState()
    data = rep.representationUsingType_properties_(4, None)  # NSBitmapImageFileTypePNG
    if data is None:
        raise SystemExit(f"cannot encode icon at {size}px")
    return bytes(data)


def main() -> int:
    ICONSET.mkdir(parents=True, exist_ok=True)
    cache: dict[int, bytes] = {}
    for name, size in ENTRIES.items():
        if size not in cache:
            cache[size] = render(size)
        (ICONSET / name).write_bytes(cache[size])
    subprocess.run(["iconutil", "-c", "icns", str(ICONSET), "-o", str(ICNS)], check=True)
    (ROOT / "assets" / "icon.png").write_bytes(cache[256])
    print(f"wrote {ICNS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
