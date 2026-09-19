# Partner logos

The strip of partner institutions on the sign-in page, the home page and
the participant welcome / thank-you screens is driven by
[`../partners.json`](../partners.json). No code change is needed to add,
remove, reorder or re-label a partner.

## Adding a logo

1. Get the file **from the organisation** — their official asset, used
   with their permission and within their brand guidelines. Don't redraw,
   recolour, crop or screenshot one.
2. Put it in this directory. **SVG** if they have one; otherwise a PNG at
   least 200 px tall with a transparent background. Prefer the horizontal
   ("lockup") version, and the full-colour one meant for light
   backgrounds — logos are shown on a white plate so they read correctly
   in both the light and the dark theme.
3. In `partners.json`, set that partner's `"logo"` to the file name, e.g.
   `"logo": "indelab.svg"`.

While `"logo"` is `null` the partner is shown as a plain text link, so
the strip is always presentable.

## Fields

| Field | |
|---|---|
| `label` (top level) | Optional small heading above the strip, e.g. `"A collaboration of"`. Leave `""` for none. The wording states a relationship — agree it with the partners. |
| `name` | Shown when there is no logo. Write institutions out **in full** ("University of Amsterdam", "Vrije Universiteit Amsterdam"), never abbreviated. |
| `caption` | Optional small line under the logo — for a lab's logo, the university it belongs to, in full. Not shown in names-only mode (the `name` already says it). |
| `title` | Full name; the logo's alt text and the link's tooltip. |
| `url` | Where the logo links to. Must be `https://`. |
| `logo` | File name in this directory, or `null`. |

## The funding line

`"funding"` (top level) is the funder acknowledgement shown under the
strip: `text` is the whole sentence, and `link_text` + `url` turn those
words of it into a link. The sentence is the funder's required
acknowledgement, grant number included, and is **pinned word for word** in
`tests/test_partners.py` — which also checks that `README.md` and
`docs/index.md` carry the same sentence. Change it in all three places,
and the test, together.

`tests/test_partners.py` checks the file is well-formed and that every
logo it names exists. These two paths are deliberately not cached by the
service worker, so an edit shows up on the next page load.
