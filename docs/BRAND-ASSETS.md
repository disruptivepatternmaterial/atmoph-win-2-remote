# Brand assets

`custom_components/atmoph_window/brand/` holds `icon.png` (256x256),
`icon@2x.png` (512x512), and square `logo` variants for the `atmoph_window`
domain.

## The path is prescribed, not chosen

`hacs/action` reads `custom_components/<domain>/brand/icon.png` — `brand`
singular, inside the integration directory — and only falls back to the
[`home-assistant/brands`](https://github.com/home-assistant/brands) repository
when that file is missing. Its own log states the path it looked for:

```
The repository does not contain brands assets at
custom_components/atmoph_window/brand/icon.png. Falling back to checking the
brands repository.
```

A root-level `brands/` directory — which is what the convention looks like from
the outside, and what the one other public Atmoph integration uses — is never
read. That is worth recording, because getting it wrong fails the check in a
way whose message points at the upstream repository rather than at the path.

## This does not fix the icon in the Home Assistant UI

Two different consumers, two different sources:

| Consumer | Reads from | Satisfied? |
|---|---|---|
| HACS `brands` validation | this directory | Yes |
| Home Assistant device page | `brands.home-assistant.io` | No — needs the upstream pull request |

So the device page shows a generic puzzle piece until an entry lands in
`home-assistant/brands` under `custom_integrations/atmoph_window/`. That
submission is tracked in
[issue #10](https://github.com/disruptivepatternmaterial/atmoph-win-2-remote/issues/10).

## Regenerating

The icons are drawn programmatically so the repository carries no third-party
artwork. The Atmoph name and product design belong to Atmoph Inc., and the
other public Atmoph integration declares no license, so its assets are all
rights reserved and could not be used even as a starting point.

```sh
python3 tools/generate_brand.py
```

The design is deliberately plain: a blue window frame with four panes, and
three Bluetooth radio arcs to its right. It is a functional placeholder — good
enough to pass validation and to be recognisable in a list, not a designed
mark.
