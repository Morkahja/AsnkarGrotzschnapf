# Development Task 009 — Print resolution and export quality

Status: verified.

## Standards

Print quality is the pixel count divided by the intended physical side. A DPI label is not resolution.

| Quality | Effective resolution | Classification |
|---|---|---|
| High | 300 PPI or greater | Excellent |
| Standard | 200–299 PPI | Recommended |
| Artistic | 150–199 PPI | Acceptable |
| Low | Below 150 PPI | Not print-ready at that size |

Square sizes are 4, 6, 8, 10, and 12 inches. Portrait and landscape use those lengths on the long edge and keep their aspect ratio. A size below 150 PPI is marked unsuitable and is not offered.

Checked figures: 1024×1024 is 171 PPI at 6×6 inches (acceptable) and 128 PPI at 8×8 inches (unsuitable). 1600×1600 is 200 PPI at 8×8 inches (standard). 2048×2048 is 341 PPI at 6×6 and 256 PPI at 8×8.

## Generation and upscaling

`sd-cli` accepts 1600 and 2048 because both are multiples of 8 and at most 2400. Z-Image Turbo has already produced a 2400×2400 image on this machine, so 1600 and 2048 are inside a size that generator has reached. RealVisXL V5.0 is an SDXL checkpoint reviewed at 1024×1024; a larger side is a separate generation, not a property of a 1024 file.

No ESRGAN weights are installed, and none were downloaded. ImageMagick is present. It is used only to write a quality-95 JPEG of the same pixels, and to shrink an Instagram preview when the long edge is above 1080. It is not used to enlarge a print. The approved image is copied. A new sample is not generated in its place.

## Print pack

`PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q` ran 71 tests, all passing.

One new pack: `data/publish-packs/lab-comparison-seed-88421021-082b1aae`, from `data/experiments/comparison/20261021T005115658622Z-body-seed-88421021.json`.

The source is the 1024×1024 RealVisXL image of THE LAMB. It is unchanged. The print PNG and the JPEG are 1024×1024. The PNG’s DPI label is 256, which is the density of the 4×4 inch recommended size, and the pixel count is the same. The largest recommended size is 4×4 inches at 256 PPI. The largest acceptable size is 6×6 inches at 171 PPI. Eight, ten, and twelve inches are unsuitable. The listing says the purchase is a digital download, not a physical print. The Instagram preview is the same 1024×1024 image, with no added text and no enlargement. `publication_ready` is true. `published` is false.

The earlier packs `lab-abstraction-007-seed-21-77ad334b` and `lab-production-seed-1049191551-667a1d45` were not rewritten. The 2400×2400 production print remains a separate sample from the older 300 DPI target.
