# Alfred — Slack app icon options

Nine "AI butler" icon candidates for the Alfred Slack bot, three each from
gpt-5.6-sol, grok-4.5, and fable-5 (same design brief for all three). Each
concept has editable SVG source and a 1024×1024 PNG ready to upload at
api.slack.com → the app → **Basic Information → Display Information → App icon**.

The 32px column approximates how the icon reads at sidebar size.

| Icon | 32px | Concept | Designer |
| --- | --- | --- | --- |
| <img src="sol-bowtie-chat.png" width="96"> | <img src="sol-bowtie-chat.png" width="32"> | `sol-bowtie-chat` — smiling chat-bubble face wearing a bow tie | gpt-5.6-sol |
| <img src="fable-quiet-butler.png" width="96"> | <img src="fable-quiet-butler.png" width="32"> | `fable-quiet-butler` — bowler hat + bow tie "invisible butler", antenna on the crown | fable-5 |
| <img src="fable-monocle-ping.png" width="96"> | <img src="fable-monocle-ping.png" width="32"> | `fable-monocle-ping` — monocled butler face knocked out of a chat bubble, chain as circuit nodes | fable-5 |
| <img src="fable-bow-tie-monogram.png" width="96"> | <img src="fable-bow-tie-monogram.png" width="32"> | `fable-bow-tie-monogram` — stylized "A" wearing a bow tie, antenna at the apex | fable-5 |
| <img src="sol-monocle-circuit.png" width="96"> | <img src="sol-monocle-circuit.png" width="32"> | `sol-monocle-circuit` — hatted face with a circuit-trace monocle | gpt-5.6-sol |
| <img src="grok-circuit-bowtie.png" width="96"> | <img src="grok-circuit-bowtie.png" width="32"> | `grok-circuit-bowtie` — abstract bow tie as a circuit with node dots | grok-4.5 |
| <img src="sol-cloche-signal.png" width="96"> | <img src="sol-cloche-signal.png" width="32"> | `sol-cloche-signal` — gloved hand serving a cloche with antenna handle | gpt-5.6-sol |
| <img src="grok-cloche-bubble.png" width="96"> | <img src="grok-cloche-bubble.png" width="32"> | `grok-cloche-bubble` — serving cloche with a chat-bubble accent | grok-4.5 |
| <img src="grok-bowler-monocle.png" width="96"> | <img src="grok-bowler-monocle.png" width="32"> | `grok-bowler-monocle` — dark bowler hat with a cyan monocle eye | grok-4.5 |

To regenerate a PNG from its SVG source:

```sh
rsvg-convert -w 1024 -h 1024 assets/alfred-icons/<name>.svg -o assets/alfred-icons/<name>.png
```
