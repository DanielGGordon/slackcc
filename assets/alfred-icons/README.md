# Alfred — Slack app icon

The Alfred bot's app icon: a monocled butler face knocked out of a chat
bubble, with the monocle chain doubling as a trail of circuit nodes.

<img src="alfred-icon.png" width="128"> <img src="alfred-icon.png" width="32">

Chosen from nine AI-butler candidates (three concepts each from gpt-5.6-sol,
grok-4.5, and fable-5 — this is fable-5's `monocle-ping`); the full set lives
in the history of PR #7.

`alfred-icon.svg` is the editable source; `alfred-icon.png` is the 1024×1024
render to upload at api.slack.com → the app → **Basic Information → Display
Information → App icon**. To regenerate the PNG after editing the SVG:

```sh
rsvg-convert -w 1024 -h 1024 assets/alfred-icons/alfred-icon.svg -o assets/alfred-icons/alfred-icon.png
```
