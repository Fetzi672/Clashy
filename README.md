# Clashy

A Clash of Clans bot for one Discord server and one clan.

## Features
- **Self-updating panels** that are edited in place and never reposted: clan statistics, member list, top 10, clan games, donations, war and CWL performance, base layouts.
- **Live war log** (`/warlog`): every attack and defense as one line, plus matchup, start and end. Works for normal wars and CWL.
- **Clan logs per channel** (`/clanlog`): pick what each channel gets: war, joins, leaves, donations, town hall/troop/hero/spell/equipment upgrades, name, role and league changes, Supercell maintenance start/end.
- **Emoji database**: town halls, troops, spells, heroes and equipment as application emojis (`backend/emojis.json`).

## Reliability
- One shared state file, saved atomically with a backup (`state_store.py`).
- A panel is only posted again after Discord confirms the old message is gone (`panels.py`).
- Clash API calls have timeouts, bounded retries and a short cache. Temporary errors keep the last good panel instead of showing empty data (`coc_api.py`).
- Background loops cannot die from one error or hang forever.
- Log messages use Discord nonces, so a crash can't post one twice.

## Setup
1. Create a Discord application and bot, then invite it with *Send Messages*, *Embed Links*, *Attach Files*, *Read Message History* and *Use External Emojis*. It does not need Administrator.
2. Create a Clash of Clans API token for your server's IP.
3. `cp .env.example .env` and fill it in.
4. `docker compose up -d --build`
5. Run the setup commands in the channels you want, e.g. `/warlog` and `/clanlog`.

The emoji IDs in `backend/emojis.json` belong to the original bot application. On your own bot, upload the emojis to your application and replace the IDs. Unknown emojis fall back to plain text, so nothing breaks either way.

## Tests
```
cd backend
pip install -r requirements.txt pytest pytest-asyncio
python -m pytest -q tests test_pacing.py
```

## Notes
This material is unofficial and is not endorsed by Supercell. For more information, see Supercell's Fan Content Policy: https://supercell.com/en/fan-content-policy/

## License
MIT, see [LICENSE](LICENSE). The bundled font Lilita One keeps its own license: SIL Open Font License 1.1 (`backend/assets/fonts/LilitaOne-OFL.txt`).
