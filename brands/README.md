# Brand assets

`icon.png` (256x256) and `icon@2x.png` (512x512) for the `atmoph_window` domain.

## Why they live here

Two different consumers want them, for different reasons:

- **HACS validation** accepts a brand directory in the repository, falling back
  to [`home-assistant/brands`](https://github.com/home-assistant/brands) when
  there is none. Having these files here is what lets the `brands` check pass
  without an accepted upstream pull request.
- **The Home Assistant UI** never loads icons from an integration folder. It
  loads them from `brands.home-assistant.io`, so the device page shows a
  generic puzzle piece until the upstream entry lands. That submission is
  tracked in [issue #10](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/10).

## Submitting upstream

Open a pull request against `home-assistant/brands` adding both files under
`custom_integrations/atmoph_window/`. The domain must match `manifest.json`.

## Regenerating

The icons are drawn programmatically so the repository carries no third-party
artwork — the Atmoph name and product design belong to Atmoph Inc., and the
one other public Atmoph integration declares no license, so its assets could
not be reused:

```sh
python3 tools/generate_brand.py
```

The design is deliberately plain: a blue window frame with four panes, and
three Bluetooth radio arcs to the right of it.
