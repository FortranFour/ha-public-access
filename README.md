<div align="center">
  <img src="https://raw.githubusercontent.com/FortranFour/ha-public-access/main/custom_components/public_access/brand/icon@2x.png" width="112" alt="Public Access logo" />
  <h1>Public Access</h1>
  <p><strong>Share a Home Assistant dashboard with anyone — read-only, on your own address, with no login.</strong></p>
  <p>Your real dashboard, live, behind glass: its tabs, its 3D floor plan, its cameras. Visitors see it as you designed it and cannot change anything.</p>

![Home Assistant 2026.1+](https://img.shields.io/badge/Home%20Assistant-2026.1%2B-18bcf2?style=flat-square)
[![HACS custom](https://img.shields.io/badge/HACS-custom%20repository-41BDF5?style=flat-square)](https://hacs.xyz/)
[![License MIT](https://img.shields.io/badge/license-MIT-2f855a?style=flat-square)](LICENSE)
</div>

> [!NOTE]
> This is a fork of [dllfpp/ha-public-access](https://github.com/dllfpp/ha-public-access), which publishes **one view** of a dashboard. The mirror, the read-only design and most of the code are dllfpp's work; if it is useful to you, [their donation link](https://buymeacoffee.com/dllfpp) is the right place to say so. The fork adds what a whole-house dashboard needs: several views with the tab bar, cards that reuse cards from other tabs, 3D floor plans, cameras and Frigate. It installs under the same name, so it replaces the original rather than sitting beside it.

<img width="1280" alt="A published dashboard: the tab bar, and a 3D floor plan with a room's controls open" src="https://raw.githubusercontent.com/FortranFour/ha-public-access/main/docs/screenshots/tabs-floorplan.png" />

## What the fork adds

| | |
| --- | --- |
| **Several views** | Pick any number of the dashboard's tabs. Visitors get Home Assistant's own tab bar for them, without the menu, edit, search and assistant buttons. Subviews keep their back arrow. With one view, or with the tab bar switched off, the page is the view and nothing else, as before. |
| **Links between views** | A card that navigates to `/your-dashboard/lights` goes to `/your-public-path/lights`. The dashboard's real address and name are replaced before anything is sent; a public title replaces them in the browser tab. Navigation to anything outside the published dashboard is not made. |
| **Supporting views** | Views that are sent but have no tab, for cards that reuse something kept on another tab: [Dashboard References](https://github.com/FortranFour/dashboard-references) cards, popups, a floor plan whose room panels take their entities from source cards elsewhere. |
| **3D floor plans** | [floor3dx-card](https://github.com/FortranFour/floor3dx-card) and its rooms card draw as they do for you: templated titles and readouts, room panels, pictures on model surfaces, entities named only inside a template. Other cards of the floor3d family ask Home Assistant for the same things. |
| **UIX** | [UIX](https://github.com/Lint-Free-Technology/uix) Forge cards, badges and rows draw, with their macros, billets, locks and foundries. Only the foundries a published view uses are sent. |
| **Cards that write their own templates** | A template some card assembles in the browser (a [Dashboard References](https://github.com/FortranFour/dashboard-references) token filled in, for one) is rendered as long as it reads only published entities. |
| **Maps, energy, LLM Vision** | Map cards get their background tiles. Energy and power cards get the sensors the energy settings list. The [LLM Vision card](https://github.com/valentinfrlch/llmvision-card) gets the timeline of published cameras, with pictures. |
| **Cameras** | Pictures always; live streams (HLS, WebRTC) for cameras on a published view, unless switched off. |
| **Frigate** | Cards can list events and reviews, show thumbnails and snapshots, play clips and recordings, and open live streams, for published cameras only. Works with the [Advanced Camera Card](https://github.com/dermotduffy/advanced-camera-card) and the [Wildlife Card](https://github.com/FortranFour/wildlife-card). |
| **Less about your home** | Visitors' browsers no longer receive the home's coordinates and name, folders and addresses of the installation, devices that are not on a published view, or any device's MAC address, serial number and configuration link. Logbook lines do not say who or what caused them. |

## Install with HACS

Requires **Home Assistant 2026.1 or later**.

1. In HACS open **⋮ → Custom repositories** and add `https://github.com/FortranFour/ha-public-access` with category **Integration**. If the original Public Access is installed, remove its repository from HACS first; your configuration is kept.
2. Download **Public Access** and restart Home Assistant.
3. Go to **Settings → Devices & services → Add integration → Public Access**, then pick the dashboard, the views and the public path. An existing entry keeps publishing its one view until you add more in **Configure**.
4. Open the public link in a private window to see exactly what your visitors see.

> **Publish on purpose.** Everything on a published view is visible to anyone who has the link, and so is everything on a supporting view to a visitor who looks for it. There is no password: the public path is the only thing between a stranger and the page, so for a dashboard with cameras choose a long one nobody would guess (`house_7c41f09be2` rather than `public`), share the link only with whom you mean to, and switch the integration off when the visit is over.

## The things you choose

Say Home Assistant is at `https://home.example.com` and the dashboard **House** (`dashboard-house`) has the tabs *Summary*, *Controls*, *Security*, *3D* and *Workshop*.

| | What it is | In the example |
| --- | --- | --- |
| **The views** | *What* is public: the tabs visitors get. Every other view, and every other dashboard, is not even sent to the visitor's browser. | *Summary*, *Security*, *3D* |
| **Supporting views** | Sent without a tab, so cards on the published views can find what they refer to. | *Controls*, if *3D*'s room panels take their entities from cards there |
| **The public path** | *Where* it is public: the word after your Home Assistant address. | `house_7c41f09be2` → `https://home.example.com/house_7c41f09be2` |
| **The public title** | What the browser tab says instead of the dashboard's and the home's own names. | `Lake house` |

> A view can be picked if it has an address of its own (the view's settings, *URL*), except the first view of the dashboard, which always can.

The public path uses lowercase letters, digits and underscores, with **no hyphen**: every Home Assistant dashboard address contains a hyphen, so a public path without one can never clash with a dashboard of yours. Paths Home Assistant already uses (`config`, `energy`, `history`, …) are refused.

## Options

| Option | Default | What it does |
| --- | --- | --- |
| Serve the public dashboard | on | Off: the address answers "not found", as if never configured |
| Dashboard, views to publish | — | What is public |
| Supporting views | — | Sent, but not shown as tabs |
| Show the tab bar | on | Off: no header at all, even with several views; cards still navigate between them |
| Public title | the dashboard's name | Shown in the browser tab and as the installation's name |
| Public path | `public` | Where it is public. A change takes effect after a restart |
| Ask search engines not to index it | on | Keeps the page out of Google and friends |
| Keep the home's coordinates from visitors | on | Visitors' browsers are told the home is at 0, 0. Off: they get the real coordinates, which a card that centres on the home (a weather radar) needs |
| Let visitors start live camera streams | on | Off: cameras on published views show still pictures; no HLS or WebRTC stream is started for a visitor |
| Frigate cameras visitors may browse | — | Adds cameras to the ones found on the published views (see below). Offered when the Frigate integration is installed |
| Allowed embedding origins | — | Sites allowed to show the page in a frame |
| Loading screen colour | — | A hex colour for Home Assistant's launch screen, for wall screens |

## Cameras and Frigate

A camera entity on a published view can be seen: its picture, and its live stream if streams are on. That needs no setting.

For Frigate, a camera is **published** when any of these holds:

- its camera entity is on a published or supporting view;
- its Frigate name is written in a card there (`cameras: [Driveway, Front]`);
- it is listed under **Frigate cameras visitors may browse** in the options. Use this for a card that shows every camera without naming any.

For published cameras, cards can list events and reviews, receive new ones as they happen, and open thumbnails, snapshots, clips, recordings and live streams. For every other camera each of those is refused, whatever the card asks for:

- event and review lists are asked for published cameras only, and filtered again when Frigate answers;
- a thumbnail, snapshot or clip is served only for an event that was first listed to a visitor that way, so guessing an event id opens nothing;
- recordings, previews and live streams are checked by camera name;
- marking a review as seen and keeping an event are changes, and stay refused like every other change;
- go2rtc's own stream list, which can show camera logins, is never served. The description of one published stream, which camera cards read to learn whether it has sound, is served without the camera's address on your network and without who else is watching.

Home Assistant gives every camera and image entity a token in its state, which opens that entity's snapshot (and, for cameras that have one, its MJPEG stream). A visitor's browser needs that token to draw the picture, so it has it; it rotates every few minutes and opens nothing else.

The Frigate integration's notification links (`/api/frigate/notifications/<event>/…`) are public by its own design unless you switch that off in the Frigate integration's options. Public Access neither adds to nor takes from that.

## Security

- **Only reading is forwarded.** The visitor's page talks to Public Access, not to Home Assistant, and only messages that *read* are passed on. Commands, saves, scripts and arbitrary templates are refused.
- **A read-only user underneath.** What is passed on runs as a system user in Home Assistant's own read-only group, so a message that slipped through would be refused by Home Assistant.
- **Only what the views use.** A visitor can read the entities written in the published and supporting views (as an entity, or inside a template there), their history and logbook, and their devices' names.
- **Templates are yours, or watched.** A template is rendered as it is only if you wrote it for this page: its text is in a published or supporting view word for word, or in a UIX foundry such a view uses, or it is exactly what UIX composes from those (your macros and billets in front of your template). Any other template a card assembles in the browser is rendered under watch: Home Assistant reports which entities a template read, and the result reaches the visitor only if every one of them is published. A watched template that walks through a whole domain or through everything fails that test. One that lists entities or devices by area, label, device or integration, reads device details, or measures a distance from home is not rendered at all, since those read the house without reading any entity. A watched template gets five seconds.
- **Your template, your entity.** A template of yours that takes its entity from the card (`{{ states(entity) }}`) is not rendered when a visitor sends it with another entity that is not published.
- **Signed links are checked.** Cards get pictures and video through short-lived signed links. Home Assistant would sign any path for a logged-in user; for a visitor, Public Access signs only a published camera's or image's picture, a published calendar, and Frigate media of published cameras. The rule is in [`policy.py`](custom_components/public_access/policy.py), with its tests in [`tests/test_policy.py`](tests/test_policy.py).
- **REST requests stay home too.** Anything a card fetches from `/api/…` is steered to the public endpoint, which answers under the same rule and otherwise says "not found". Nothing reaches Home Assistant with the page's placeholder login.
- **Your own login is untouched.** The public page never writes to the browser storage your Home Assistant uses.
- **Auditable.** All the code is in this repository. Nothing is downloaded at runtime.

UIX sends its frontend its foundries and its Broker rules. A visitor gets the foundries the published views use, and the Broker rules as they are; both arrive with any `!secret` in them already filled in, so keep secrets out of those.

What a visitor still learns besides the views: which integrations are loaded, the names of your areas, floors, labels and themes, your units, language and time zone, and the dashboard's resources (custom cards). Diagnostics (**Settings → Devices & services → Public Access → ⋮ → Download diagnostics**) list exactly which views, entities and Frigate cameras are open.

The [`tests/`](tests) folder holds the automated checks, among them that the integration contains no code able to write to Home Assistant, that a missing view publishes nothing rather than another view, and that nothing is signed without asking the policy.

## What was tested, and what was not

Tested on Home Assistant 2026.11.1 in headless Chrome, with a dashboard built for the purpose: tab bar and navigation, subviews, references across views, floor3dx-card 2.19.5 and its rooms card, Dashboard References 0.1.0-beta.3, Wildlife Card 1.3.4, Advanced Camera Card 8.2.0, UIX 9.0.0 (Forge cards and badges, foundries, the lock spark), Home Assistant's own map and energy cards, LLM Vision Card 1.7.2 against a stand-in for its timeline, and a list of things a hostile visitor might send.

Frigate was a stand-in that answers like the Frigate integration (same messages, same addresses, canned events), not a Frigate server. So the rules were exercised, and the cards' lists, thumbnails and pushed events worked through them, but **playing a clip or a recording and live video (HLS, WebRTC, go2rtc/MSE) have not been seen working end to end**. Try them on your own installation before relying on them, and please report what you find.

A UIX lock keeps its place on the public page, but a code written in the card is sent to the visitor's browser with the rest of the card, as it is to any of your own users. Nothing behind the lock can be operated from the public page either way; just do not use a code there that opens anything real.

Not supported: cards that pick their entities at run time from everything in Home Assistant (auto-entities, area and strategy dashboards), since only entities written in the views are sent; popups opened by a script or an automation, since those are service calls; Frigate's own web interface.

## Limits

| | |
| --- | --- |
| **Visitors at once** | Up to 25, and 4 per address; past that, new visitors are asked to retry |
| **Requests** | 60 pages a minute per visitor address, and 600 pictures or clips |
| **What leaves your home** | Nothing: no account, no license server, no telemetry |

## Behind Cloudflare or Nginx Proxy Manager

**Nothing to configure.** Public Access tells visitors apart by itself: behind Cloudflare it reads the visitor's address from the `CF-Connecting-IP` header that Cloudflare sets (and only when the connection really comes from Cloudflare), behind your own front server, such as Nginx Proxy Manager, from the address that server recorded.

Live video is another matter: WebRTC needs a route from the visitor to your cameras or go2rtc that a web proxy does not provide, so through Cloudflare or Nabu Casa expect HLS or still pictures unless you have set that up for your own use already.

## Troubleshooting

| Problem | What to do |
| --- | --- |
| **The address shows "not found"** | The integration is switched off in its options, or you changed the public path: a new path starts working after a Home Assistant restart. |
| **My dashboard is not in the list** | Only dashboards saved at least once can be published. Open it, make any change, save. |
| **A view is not in the list** | Give it an address: open the view's settings and fill in *URL*. |
| **A card says it cannot find its source, or a popup is empty** | What it refers to is on a view that is not sent. Add that view under **Supporting views**. |
| **A card is blank, shows "entity not found", a template warning or "could not sign"** | Open the public page so the fault happens, then download diagnostics. Three lists there say what happened: `page_errors` is what went wrong inside the page itself (a card that threw and where, a script that did not load, an error card and its text); `recently_refused` is what the page asked for and did not get, with the reason; `refused_requests` counts every kind of request turned down (`call_service` there is only a visitor pressing a button). An entity chosen at run time, or one that comes from another dashboard, is not open; diagnostics list the ones that are. |
| **Labels in the settings form are raw names (`support_views`, `show_tabs`)** | Home Assistant is still holding the previous version's texts. Restart it. |
| **A weather radar or another map shows open sea** | It centres on the home, and visitors are told the home is at 0, 0. Switch off **Keep the home's coordinates from visitors**. |
| **A Frigate card shows no events** | Its cameras are not published: list them under **Frigate cameras visitors may browse**. |
| **The page loads forever** | Reload with the cache cleared (Ctrl/Cmd + Shift + R). |

## Development

```bash
git clone https://github.com/FortranFour/ha-public-access.git
cd ha-public-access
python -m pytest tests -q
```

The tests that need Home Assistant are skipped without it; `pip install homeassistant` runs them all.

Embedding the page in your own site works as in the original: drop `X-Frame-Options` at the server in front of Home Assistant on the public path, and set **Allowed embedding origins**.

## License

MIT, see [LICENSE](LICENSE). Original work © dllfpp; fork changes © FortranFour.

---

<sub>Not affiliated with the Home Assistant project, the Open Home Foundation, Nabu Casa, Frigate or the original author.</sub>
