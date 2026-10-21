# Development Task 010 — Generate the print at its final size

Status: verified.

## Decision

The artwork is generated once, at the size that will be approved and published. The production default is 1600×1600, an 8×8 inch print at 200 PPI. Print preparation copies that file. It does not upscale it and it does not generate a second image.

`--print-inches` asks for another square side at 200 PPI. Each side is the inches times 200, rounded up to a multiple of 8, so the file is never smaller than the requested print. 8, 10, and 12 inches are 1600, 2000, and 2400 pixels. A size above 2400 is rejected. `--width` and `--height` stay exact pixels. Passing both a print size and a pixel size is an error. The prompt lab accepts the same range, 128 through 2400, and no longer stops at 1024.

A side longer than 1024 adds VAE tiling. That is a memory strategy. It does not change the pixel count. Z-Image, RealVisXL, and DreamShaper keep their own sizes.

## Native 1600 generation

One Z-Image Turbo image was generated with the production settings: 8 Euler steps, CFG 1.0, Vulkan, CPU offload, and VAE tiling. The prompt was a short meadow sentence, seed 1600200. It was not `generate --pending`, so the existing THE LAMB artwork was left in place.

The run finished in 99.1 seconds and wrote a real 1600×1600 PNG:

`data/experiments/print-1600/20261021T031805199909Z-body-seed-1600200.png`

The picture is a lamb in a meadow. It is not noise and it is not a smaller image padded up. The metadata records an intended print of 8×8 inches at 200 PPI. That file's `vae_tiling` field still says false, because it stored the model profile. Later experiment records store whether tiling was actually added. The saved file was left as written.

The print pack `data/publish-packs/lab-print-1600-seed-1600200-7f8dc631` copies that file. The print PNG and JPEG are 1600×1600. The resizing method is `none`. The Instagram and Etsy previews are 1080×1080, reduced from the same artwork. Nothing was published. The pack is not publication-ready because the sentence is not an archived poem.

Earlier experiments, the THE LAMB production PNG, and the older publish packs were not rewritten.

## Tests

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q` ran 74 tests, all passing.
