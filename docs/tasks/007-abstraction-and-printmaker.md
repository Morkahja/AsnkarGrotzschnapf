# Development Task 007 — Abstraction and the local Printmaker

Status: implemented.

## Artistic direction

The Ollama interpreter now builds a picture by a construction method. Gentle keeps one nameable subject and breaks realism in a single structural way. Surreal Abstract, the default, invents a visible metaphor instead of illustrating the text in order. Experimental starts from color, shape, and movement. Dreamlike, Painterly, and Symbolic still change the surface and can be combined with any level.

Z-Image and RealVisXL default to a negative prompt that discourages photographs and lettering. Z-Image Turbo leaves the negative prompt off because its CFG does not use one. The field stays editable.

Production generation is unchanged.

## Printmaker

`anskar print` and the lab's Approve for print button copy one saved experiment into `data/publish-packs/<artwork-id>/`. The original file stays where it was. A square 512 image is generated again at 1024. Portrait and landscape keep their shape. No learned upscaler is installed, and ImageMagick interpolation is not used. The DPI label does not add pixels.

Instagram and Etsy drafts are written beside the print. A pack is publication-ready only when the source text matches an archived poem with documented Swedish commercial-use rights. Nothing is published.

Surreal Abstract and Experimental get one revision pass so a draft that still depicts the source's creature is rewritten before it is saved.

## Verification

65 unit tests passed. The same poem, THE LAMB, was interpreted with Z-Image Turbo at 512×512 and seed 21. The earlier Painterly interpretation remains `data/experiments/interpretation-006/20261021T014536042537Z-interpreted-body-seed-21.png`. The comparison set is the later files in `data/experiments/abstraction-007/`: gentle `20261021T021457509490Z`, surreal `20261021T021722822117Z`, experimental `20261021T021742749657Z`. Earlier attempts in that folder were kept.

The surreal image was approved in the prompt lab. The pack is `data/publish-packs/lab-abstraction-007-seed-21-77ad334b/`. The original stays 512×512. The print file is 1024×1024. It is publication-ready and unpublished. A second approval was refused. The production artwork was not regenerated.
