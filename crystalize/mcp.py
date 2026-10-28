"""Crystalize as a Model Context Protocol server, so an agent can DJ in FUSER.

The protocol is spoken by hand, newline-delimited JSON-RPC over stdin and stdout, as
bejeweled does: the tools need a few dozen lines of it, and Crystalize has no
dependencies otherwise.

Decks are 1 to 4 here, as a person counts them, and 0 to 3 in the game.
"""
from __future__ import annotations

import json
import sys
import threading
import time

from . import __version__, music, store
from .bridge import GameError
from .mixes import Plan
from .player import REMEMBERED_DISCS as REMEMBERED, Player

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

INSTRUCTIONS = (
    "Crystalize plays FUSER, Harmonix's DJ game, through a mod running inside it. FUSER has "
    "four decks, numbered 1 to 4. Each plays one disc, a single part of a song: its beat, "
    "bass, loop or lead, the lead usually being the vocal. FUSER conforms every disc to the "
    "session's key and tempo and brings it in on the beat. That makes any two discs play "
    "together, not sound good together: a mix is planned with plan_mix, whose checks catch "
    "what cannot be heard from here, and played with play_mix, which keeps time. The "
    "build_mix prompt sets out the steps. Keys are written Ab for major and Abm for minor."
)

# A failed attempt to reach FUSER costs a second and a half, so while it is closed the
# server plans from the saved library and tries the game again only this often
OFFLINE_RETRY_SECONDS = 30

# How often await_event reads the set. A read is one round trip, about 40 ms, and shares the
# bridge with the player's clock, so twice a second leaves the clock most of it
AWAIT_POLL_SECONDS = 0.5
# Longest an await_event call blocks, under what MCP clients wait for a tool before giving up
AWAIT_MAX_SECONDS = 60
# Unanswered reads in a row before await_event gives up on FUSER
AWAIT_MISSES = 3
# A request this many measures from its end is reported as ending: about 8 seconds at 120
# BPM, time for a search and a drop timed to the downbeat
ENDING_MEASURES = 4
# FShowScoring's fields, by the names get_set gives them
POINTS = {"requests": "RequestPoints", "request_combos": "RequestComboBonusPoints",
          "goals": "ShowGoalPoints", "perfect_timing": "PerfectTimingBonusPoints",
          "early_timing": "EarlyTimingBonusPoints", "late_timing": "LateTimingBonusPoints",
          "bad_timing": "BadTimingBonusPoints", "crowd_meter": "CrowdMeterPoints"}
# The meter level await_event warns below unless told otherwise. It drains a point a
# second once idle, so this is half a minute from failing the set
METER_LOW = 30

DECK = {"type": "integer", "minimum": 1, "maximum": 4, "description": "1 to 4"}
KEY = {"type": "string", "description": "Ab for A flat major, Abm for minor"}

STEP = {
    "type": "object",
    "properties": {
        "bar": {"type": "integer", "description": "From 0, the bar the opening discs land on"},
        "do": {"type": "string", "enum": ["drop", "eject", "key", "tempo", "mute", "unmute"]},
        "deck": DECK,
        "disc": {"type": "string", "description": "For drop: a disc name from search_songs"},
        "key": KEY,
        "bpm": {"type": "integer"},
    },
    "required": ["bar", "do"],
}

PLAN = {
    "key": {**KEY, "description": "The opening key, " + KEY["description"]},
    "bpm": {"type": "integer", "description": "The opening tempo"},
    "steps": {"type": "array", "items": STEP,
              "description": "What happens on which bar. Steps on one bar happen together"},
    "end_bar": {"type": "integer",
                "description": "The bar the mix ends on. Defaults to 16 after the last step"},
}

TOOLS = [
    {
        "name": "get_state",
        "description": "The session's key and tempo, the tempo range FUSER allows, each deck's "
                       "playing and queued disc, and the mix playing, if one is: the bar it is "
                       "on and the bar its next step is on.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_set",
        "description": "In a campaign set: the measure FUSER is on, the open crowd requests "
                       "with what each wants and the measure it ends on, the goals not yet "
                       "done with their progress, how many of each are settled, whether the "
                       "section has a crowd meter, the meter from 0 to 100 and its band (low, "
                       "medium, high, peak) and warnings, the "
                       "mix's freshness from 0 to 100, which a new drop raises and a still "
                       "stretch lets fade, and the score. Measures are FUSER's own, not a "
                       "mix's bars. A goal's text may keep a placeholder such as {Transition} "
                       "the mod cannot read yet.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "await_event",
        "description": "In a campaign set, wait until something happens and say what: a "
                       "request coming, starting, ending within a few measures, passing or "
                       "failing; a goal starting, done, or ending undone; the crowd meter "
                       "coming on or falling below `meter_below`; the set ending; keep_alive "
                       "stopping. Returns at once with anything since you last looked, else "
                       "waits up to `seconds`, and gives get_set's view of the set with it. "
                       "Call it between actions instead of polling get_set.",
        "inputSchema": {"type": "object", "properties": {
            "seconds": {"type": "number", "description": f"Default 20, at most {AWAIT_MAX_SECONDS}"},
            "meter_below": {"type": "integer", "description": f"Default {METER_LOW}"},
        }},
    },
    {
        "name": "search_songs",
        "description": "Search the songs you can play, custom songs included and DLC you do "
                       "not own left out. Returns the total that match and up to `limit` of "
                       "them: id, title, artist, year, genre, the song's own BPM and key, its "
                       "tags, and its discs with name, type and instrument. Instrument, disc type "
                       "and years find what a crowd request or goal asks for. Works from the saved "
                       "library while FUSER is closed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string",
                          "description": "Words that must all appear in the title or artist"},
                "genre": {"type": "string", "enum": [g for g in music.GENRES if g]},
                "key": {"type": "string", "description": "Exact key, such as Ab or F#m"},
                "bpm_min": {"type": "number"},
                "bpm_max": {"type": "number"},
                "year_min": {"type": "integer", "description": "Released this year or later"},
                "year_max": {"type": "integer"},
                "instrument": {"type": "string", "enum": [i for i in music.INSTRUMENTS if i],
                               "description": "Only songs with a disc played on it, listing "
                                              "only those discs. FUSER names it exactly: a "
                                              "Bass Guitar disc is not a Bass one"},
                "disc_type": {"type": "string", "enum": list(music.DISC_TYPES),
                              "description": "Only songs with a disc of this type, listing only "
                                             "those discs; with instrument, a disc must be both"},
                "near": {"type": "string",
                         "description": "A song id: only songs in the same, relative or a "
                                        "neighbouring key and within 8% of its tempo, or of "
                                        "half or double it, which FUSER shifts and stretches "
                                        "least to play beside it. It narrows the library and "
                                        "does not choose: most of what it returns will not "
                                        "belong"},
                "energy_min": {"type": "integer", "description": "Lowest energy tag, 1 to 10"},
                "energy_max": {"type": "integer"},
                "untagged": {"type": "boolean", "description": "Only songs with no tags yet"},
                "limit": {"type": "integer", "description": "Default 25"},
                "offset": {"type": "integer", "description": "Skip this many, for the next page"},
            },
        },
    },
    {
        "name": "tag_songs",
        "description": "Write down what songs are like to hear, for every later session to "
                       "search: energy from 1, a still ballad, to 10, the hardest in the "
                       "library; style in a few words; what the lyrics are about; and "
                       "mood_clash when the sound and the words pull opposite ways. Fields "
                       "given replace those there; clear removes a song's tags.",
        "inputSchema": {
            "type": "object",
            "properties": {"songs": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "A song id from search_songs"},
                    "energy": {"type": "integer", "minimum": 1, "maximum": 10},
                    "style": {"type": "string"},
                    "lyrics": {"type": "string"},
                    "mood_clash": {"type": "boolean"},
                    "clear": {"type": "boolean"},
                },
                "required": ["id"],
            }}},
            "required": ["songs"],
        },
    },
    {
        "name": "plan_mix",
        "description": "Check a mix without playing it. Returns its length and the timeline in "
                       "segments, each with the bars it covers, the time it starts, the key, "
                       "tempo and energy, what each deck plays with the pitch shift and stretch "
                       "FUSER gives it, and checks: two vocals, no beat, no bass, two basses, "
                       "one song on every deck, pitched far, no version in the session's mode, "
                       "stretched too far. Checks over the whole mix follow: steps off the "
                       "phrase, stretches where nothing changes, discs dropped twice, key "
                       "clashes, tempo and energy jumps. A plan that cannot play is refused "
                       "with every problem listed.",
        "inputSchema": {"type": "object", "properties": {
            **PLAN,
            "mix": {"type": "string", "description": "A saved mix to check, in place of a plan"},
            "from_bar": {"type": "integer", "description": "List segments from this bar only"},
            "to_bar": {"type": "integer", "description": "List segments up to this bar only"},
        }},
    },
    {
        "name": "save_mix",
        "description": "Save a plan under a name, refusing one plan_mix would refuse. Saving "
                       "the name again replaces it.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}, **PLAN},
                        "required": ["name", "key", "bpm", "steps"]},
    },
    {
        "name": "load_mix",
        "description": "A saved plan, to change and check again.",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}},
                        "required": ["name"]},
    },
    {
        "name": "list_mixes",
        "description": "The saved mixes, with when each was saved and its number of steps.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "play_mix",
        "description": "Play a saved mix or a plan in FUSER, each step on its bar, stopping any "
                       "mix already playing. Decks the plan does not fill at bar 0 are emptied "
                       "first. Returns at once; get_state follows it. Needs a freestyle session or a "
                       "campaign set.",
        "inputSchema": {"type": "object", "properties": {
            **PLAN, "mix": {"type": "string", "description": "A saved mix, in place of a plan"},
        }},
    },
    {
        "name": "stop_mix",
        "description": "Stop the mix playing. The decks keep playing what they have.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "keep_alive",
        "description": "In a campaign set, keep the crowd meter from draining while you think: "
                       "3 bars after the last change, Crystalize drops the next of these discs "
                       "on its deck, never one already on a deck or one of the last 3 dropped, "
                       "and never so often the crowd calls it overactive. Give at least 4, "
                       "discs that can swap in for each other without spoiling the mix, such "
                       "as two beats on one deck and two loops on another. It waits while a "
                       "mix plays. With `stop`, it stops.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "discs": {"type": "array", "items": {
                    "type": "object", "properties": {"disc": {"type": "string"}, "deck": DECK},
                    "required": ["disc", "deck"]}},
                "stop": {"type": "boolean"},
            },
        },
    },
    {
        "name": "drop_disc",
        "description": "Play a disc on a deck, replacing what it held, from the next bar. "
                       "Returns once it is sent, up to a bar later. With `queue`, it waits on "
                       "the deck until drop_queue.",
        "inputSchema": {
            "type": "object",
            "properties": {"disc": {"type": "string", "description": "A disc name"},
                           "deck": DECK, "queue": {"type": "boolean"},
                           "on_pickup": {"type": "boolean",
                                         "description": "Land on the disc's next pickup, "
                                                        "where it comes in ahead of the bar, "
                                                        "instead of the downbeat"}},
            "required": ["disc", "deck"],
        },
    },
    {
        "name": "drop_queue",
        "description": "Drop every queued disc together, from the next bar.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "eject",
        "description": "Stop a deck and take its disc off, at the end of the bar.",
        "inputSchema": {"type": "object", "properties": {"deck": DECK}, "required": ["deck"]},
    },
    {
        "name": "set_key",
        "description": "Change the session's key from the next bar. Every deck follows.",
        "inputSchema": {"type": "object", "properties": {"key": KEY}, "required": ["key"]},
    },
    {
        "name": "set_tempo",
        "description": "Change the session's tempo from the next bar, within the range "
                       "get_state gives.",
        "inputSchema": {"type": "object", "properties": {"bpm": {"type": "integer"}},
                        "required": ["bpm"]},
    },
    {
        "name": "mute_deck",
        "description": "Mute or unmute a deck without taking its disc off, from the next bar.",
        "inputSchema": {"type": "object", "properties": {"deck": DECK, "mute": {"type": "boolean"}},
                        "required": ["deck", "mute"]},
    },
    {
        "name": "solo_deck",
        "description": "Solo or unsolo a deck, from the next bar.",
        "inputSchema": {"type": "object", "properties": {"deck": DECK, "solo": {"type": "boolean"}},
                        "required": ["deck", "solo"]},
    },
]

# The craft a plan's checks cannot catch, given once when the agent asks for the workflow
# rather than paid for in every session's tool descriptions
MIX_CRAFT = (
    "A FUSER mix is layers from different songs: one song's beat under another's bass, a "
    "third's loop and a fourth's vocal. Keep one vocal at a time. Change something every 8 "
    "or 16 bars, always on a multiple of 8, and one or two decks at a time, so the listener "
    "hears each new part arrive; a new vocal over a familiar beat is the strongest move. "
    "Muting the beat for 4 or 8 bars and bringing it back is a breakdown and a drop. Choose "
    "the session key and tempo for the vocal, which suffers most from shifting, and change "
    "key only to the relative key or a neighbour. At 120 BPM a bar is two seconds."
)

PROMPTS = [
    {
        "name": "tag_library",
        "description": "Tag FUSER's songs with energy, style and lyrics, in a session that does "
                       "nothing else",
        "arguments": [],
        "text": lambda a: (
            "Tag Crystalize's library so later sessions can choose songs by how they sound. "
            "Search search_songs with untagged: true, 50 at a time, and tag each song you know "
            "with tag_songs: energy, style, what the lyrics are about, and mood_clash when the "
            "sound and the words pull opposite ways. Tag from what you know of the recording. "
            "Leave out a song you don't recognise rather than guess from its title, and page "
            "past the ones you left with offset. When only songs you don't know are left, say "
            "how many."),
    },
    {
        "name": "build_mix",
        "description": "Choose songs, plan a mix, fix what its checks find, then save and play it",
        "arguments": [("brief", "What the mix is for: an occasion, a style, a feeling", False),
                      ("minutes", "About how long it should run", False),
                      ("name", "A name to save it under", False)],
        "text": lambda a: (
            "Build a mix in FUSER with Crystalize"
            + (f", about {a['minutes']} minutes long" if a.get("minutes") else "")
            + (f": {a['brief']}" if a.get("brief") else "") + ".\n\n" + MIX_CRAFT + "\n\n"
            "1. get_state for the tempo range. Choose songs by what you know of them: style, "
            "era, lyrics and mood, from their tags where they have them. search_songs near a "
            "song narrows the library to what FUSER shifts least beside it; key and tempo "
            "matching doesn't make two songs belong together.\n"
            "2. Write the plan and run plan_mix. Fix every segment check and every check over "
            "the mix, or know why you are keeping it: a breakdown has no beat on purpose. Plan "
            "a long mix a stretch at a time with from_bar and to_bar.\n"
            f"3. save_mix {a.get('name') or 'it'} as you go, so the work survives a restart.\n"
            "4. play_mix it, follow it with get_state, and say what you built and why."),
    },
    {
        "name": "review_mix",
        "description": "Find and fix the weak parts of a saved mix",
        "arguments": [("name", "The saved mix, from list_mixes", True)],
        "text": lambda a: (
            f"Review the saved mix {a['name']} with Crystalize.\n\n{MIX_CRAFT}\n\n"
            f"load_mix it and run plan_mix on it. Fix each segment whose checks show trouble, "
            f"then the checks over the whole mix, and look for what the checks cannot see: a "
            f"vocal that stays too long, a song whose parts never meet another's, an ending "
            f"that just stops. save_mix {a['name']} and say what you changed and what you left."),
    },
]


class Server:
    def __init__(self, bridge, reader, writer):
        self.bridge = bridge
        self.reader, self.writer = reader, writer
        self.player = Player(bridge)
        self.client = None
        self._writing = threading.Lock()
        self._songs_lock = threading.Lock()
        self._songs: list[dict] = []
        self._discs: dict[str, dict] = {}
        self._bpm_range = None
        self._songs_session = None
        self._offline_since = None
        self._calls: list[threading.Thread] = []
        # The set as the agent last saw it, through get_set or await_event, so an event
        # that happened while it was busy is still reported once
        self._seen: dict | None = None
        self._warned: set = set()  # requests already reported as ending
        self._keep_error = None  # the keep-alive failure already reported

    # ------------------------------------------------------------------ protocol

    def run(self) -> int:
        for line in self.reader:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self._send({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "parse error"}})
                continue
            for one in message if isinstance(message, list) else [message]:
                if isinstance(one, dict):
                    self._handle(one)
        for call in self._calls:
            call.join()
        self.player.stop()
        return 0

    def _send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        with self._writing:
            self.writer.write(data)
            self.writer.flush()

    def _handle(self, message: dict) -> None:
        method, id_ = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if id_ is None or method is None:
            return

        if method == "initialize":
            info = params.get("clientInfo") or {}
            self.client = " ".join(str(v) for v in (info.get("name"), info.get("version")) if v) or None
            asked = params.get("protocolVersion")
            result = {
                "protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": "crystalize", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "prompts/list":
            result = {"prompts": [
                {"name": p["name"], "description": p["description"],
                 "arguments": [{"name": n, "description": d, "required": r}
                               for n, d, r in p["arguments"]]} for p in PROMPTS]}
        elif method == "prompts/get":
            prompt = next((p for p in PROMPTS if p["name"] == params.get("name")), None)
            given = {k: v for k, v in (params.get("arguments") or {}).items() if v}
            missing = next((n for n, _, r in prompt["arguments"] if r and n not in given),
                           None) if prompt else None
            if prompt is None or missing:
                why = f"{prompt['name']} needs {missing}" if prompt else "unknown prompt"
                self._send({"jsonrpc": "2.0", "id": id_,
                            "error": {"code": -32602, "message": why}})
                return
            result = {"description": prompt["description"], "messages": [
                {"role": "user", "content": {"type": "text", "text": prompt["text"](given)}}]}
        elif method == "tools/call":
            # On its own thread, so a ping is answered while FUSER is slow to reply
            call = threading.Thread(target=self._call, args=(id_, params))
            self._calls = [c for c in self._calls if c.is_alive()] + [call]
            call.start()
            return
        else:
            self._send({"jsonrpc": "2.0", "id": id_,
                        "error": {"code": -32601, "message": f"unknown method {method}"}})
            return
        self._send({"jsonrpc": "2.0", "id": id_, "result": result})

    def _call(self, id_, params: dict) -> None:
        name, started = params.get("name"), time.monotonic()
        tool = getattr(self, f"_tool_{name}", None) if isinstance(name, str) else None
        schema = next((t["inputSchema"] for t in TOOLS if t["name"] == name), None)
        if tool is None or schema is None:
            self._send({"jsonrpc": "2.0", "id": id_,
                        "error": {"code": -32602, "message": f"unknown tool {name}"}})
            return
        args = params.get("arguments") or {}
        try:
            # Refused rather than ignored, as pink diamond does: an argument the tool does
            # not take is a misunderstanding, and ignoring it hides one
            unknown = sorted(set(args) - set(schema["properties"]))
            if unknown:
                raise ValueError(f"{name} has no argument {', '.join(unknown)}; it takes "
                                 f"{', '.join(sorted(schema['properties'])) or 'none'}")
            text, failed = json.dumps(tool(args), ensure_ascii=False), False
        except (GameError, ValueError, KeyError) as e:
            text = str(e) if not isinstance(e, KeyError) else f"{name} needs {e.args[0]}"
            failed = True
        # A reply stays in the agent's context, so what a session costs is which tools it
        # called and how much each sent back. Clients keep stderr, so it is said there
        print(f"call {name}: {len(text.encode())} bytes{', failed' if failed else ''}, "
              f"{int((time.monotonic() - started) * 1000)} ms", file=sys.stderr, flush=True)
        self._send({"jsonrpc": "2.0", "id": id_,
                    "result": {"content": [{"type": "text", "text": text}], "isError": failed}})

    # ------------------------------------------------------------------- library

    def _library(self) -> list[dict]:
        """FUSER's songs, fetched once per game session and saved, so that while FUSER is
        closed the server plans from the last library it saw."""
        with self._songs_lock:
            stale = not self._songs or self.bridge.session != self._songs_session
            retry = self._offline_since is None or \
                time.monotonic() - self._offline_since > OFFLINE_RETRY_SECONDS
            if stale and retry:
                try:
                    discs = self.bridge.call("discs")
                    try:
                        state = self.bridge.call("state")
                        self._bpm_range = [state["bpm_min"], state["bpm_max"]]
                    except GameError:
                        pass  # out of a session, the range is the last one known
                    store.save_library(discs, self._bpm_range)
                    self._set_library(discs)
                    self._songs_session, self._offline_since = self.bridge.session, None
                except GameError:
                    saved = store.load_library()
                    if saved is None:
                        raise
                    if not self._songs:
                        self._bpm_range = saved.get("bpm_range")
                        self._set_library(saved["discs"])
                    self._offline_since = time.monotonic()
            return self._songs

    def _set_library(self, discs: list[dict]) -> None:
        self._songs = music.songs(discs)
        self._discs = music.disc_index(self._songs, discs)

    def _describe(self, name):
        if name is None:
            return None
        self._library()
        disc = self._discs.get(name)
        if disc is None:
            return {"name": name}
        return {k: disc[k] for k in ("name", "type", "song", "artist")}

    def _plan(self, args: dict) -> tuple[str | None, Plan]:
        self._library()
        if args.get("mix"):
            plan = store.load_mix(args["mix"])
            return plan["name"], Plan(plan, self._discs, self._bpm_range, store.tags())
        return None, Plan(args, self._discs, self._bpm_range, store.tags())

    # --------------------------------------------------------------------- tools

    def _tool_get_state(self, args: dict) -> dict:
        state = self.bridge.call("state")
        decks = []
        for i, deck in enumerate(state["decks"]):
            deck = deck or {}
            decks.append({"deck": i + 1, "playing": self._describe(deck.get("playing")),
                          "queued": self._describe(deck.get("queued"))})
        self._bpm_range = [state["bpm_min"], state["bpm_max"]]
        result = {"key": music.key_name(state["key"], state["mode"]), "bpm": state["bpm"],
                  "bpm_range": self._bpm_range, "decks": decks}
        mix = self.player.status()
        if mix:
            result["mix"] = {k: v for k, v in mix.items() if k != "started_beat" and v is not None}
        keeping = self.player.keep_alive_status()
        if keeping:
            if keeping.get("last"):
                keeping["last"] = {**keeping["last"], "deck": keeping["last"]["deck"] + 1}
            result["keep_alive"] = {k: v for k, v in keeping.items() if v is not None}
        return result

    def _tool_get_set(self, args: dict) -> dict:
        raw = self.bridge.call("set_state")
        self._seen = raw
        return self._set_view(raw)

    def _tool_await_event(self, args: dict) -> dict:
        seconds = args.get("seconds")
        seconds = min(max(float(20 if seconds is None else seconds), 0), AWAIT_MAX_SECONDS)
        below = args.get("meter_below", METER_LOW)
        deadline = time.monotonic() + seconds
        missed = 0
        while True:
            try:
                raw = self.bridge.call("set_state")
                missed = 0
            except GameError:
                # FUSER now and then leaves one read unanswered and takes the next
                missed += 1
                if missed >= AWAIT_MISSES:
                    raise
                time.sleep(AWAIT_POLL_SECONDS)
                continue
            events = self._events(self._seen, raw, below)
            self._seen = raw
            if events or time.monotonic() >= deadline:
                return {"events": events, "set": self._set_view(raw)}
            time.sleep(AWAIT_POLL_SECONDS)

    def _events(self, before: dict | None, after: dict, below) -> list[dict]:
        """What changed between two reads of the set, in get_set's words. Requests and
        goals are matched by their place in FUSER's lists, which only ever grow."""
        events = []
        measure = after.get("measure")
        old_requests = (before or {}).get("requests") or []
        for i, r in enumerate(after.get("requests") or []):
            was = old_requests[i].get("state") if i < len(old_requests) else None
            now = r.get("state")
            name = music._named(music.REQUEST_STATES, now)
            entry = self._request_entry(r, measure)
            key = (r.get("start_measure"), r.get("bars"))
            if was is None and name == "scheduled":
                events.append({"event": "request_coming", **entry})
            elif name == "active" and was != now:
                events.append({"event": "request_started", **entry})
            elif name in ("passed", "failed") and was is not None and \
                    music._named(music.REQUEST_STATES, was) in music.OPEN_REQUESTS:
                events.append({"event": f"request_{name}", **entry})
            left = entry.get("measures_left")
            if name == "active" and left is not None and left <= ENDING_MEASURES \
                    and key not in self._warned:
                self._warned.add(key)
                events.append({"event": "request_ending", **entry})

        old_goals = (before or {}).get("goals") or []
        titles = self._titles(after.get("goals") or [])
        for i, g in enumerate(after.get("goals") or []):
            old = old_goals[i] if i < len(old_goals) else None
            done, ended = g.get("completed") is True, g.get("active") is False
            text = self._goal_text(g, titles)
            if old is None:
                if not done and not ended:
                    events.append({"event": "goal_started", "goal": text})
            elif done and old.get("completed") is not True:
                events.append({"event": "goal_done", "goal": text})
            elif ended and not done and old.get("active") is not False:
                events.append({"event": "goal_ended", "goal": text})

        show, old_show = after.get("show") or {}, (before or {}).get("show") or {}
        if show.get("has_meter") is True and old_show.get("has_meter") is not True:
            events.append({"event": "meter_on"})
        meter, old_meter = _meter(after), _meter(before or {})
        if isinstance(below, (int, float)) and meter is not None and meter < below \
                and (old_meter is None or old_meter >= below):
            events.append({"event": "meter_low", "crowd_meter": meter})
        # The set's objects stay readable on the results screen, so the end is FUSER
        # counting the show out, or failing that the set no longer reading at all
        ending, was_ending = show.get("ending") is True, old_show.get("ending") is True
        unread = bool(before) and before.get("goals") is not None and after.get("goals") is None
        if (ending and not was_ending and before is not None) or unread:
            event = {"event": "set_ended"}
            stars = (show.get("results") or {}).get("stars")
            if isinstance(stars, (int, float)):
                event["stars"] = round(stars, 2)
            events.append(event)

        keeping = self.player.keep_alive_status() or {}
        if keeping.get("error") and keeping["error"] != self._keep_error:
            self._keep_error = keeping["error"]
            events.append({"event": "keep_alive_stopped", "error": keeping["error"]})
        return events

    def _titles(self, goals: list) -> dict:
        if not any(g.get("song") for g in goals):
            return {}
        return {song["id"]: song["title"] for song in self._library()}

    def _request_entry(self, r: dict, measure) -> dict:
        state = music._named(music.REQUEST_STATES, r.get("state"))
        start, bars = r.get("start_measure"), r.get("bars")
        ends = start + bars if isinstance(start, int) and isinstance(bars, int) else None
        f = r.get("filter")
        titles = {song["id"]: song["title"] for song in self._library()} \
            if f and f.get("songs") else {}
        entry = {"state": state, "wants": music.request_wants(f, titles),
                 "starts": start, "ends": ends}
        if state == "active" and ends is not None and isinstance(measure, int):
            entry["measures_left"] = ends - measure
        return {k: v for k, v in entry.items() if v is not None}

    @staticmethod
    def _goal_text(g: dict, titles: dict) -> str | None:
        return music.goal_text(g.get("name"), titles.get(g.get("song"), g.get("song")),
                               g.get("filters")) or g.get("description")

    def _set_view(self, raw: dict) -> dict:
        measure = raw.get("measure")
        result: dict = {"measure": measure}

        if raw.get("requests") is not None:
            open_, settled = [], {"passed": 0, "failed": 0}
            for r in raw["requests"]:
                state = music._named(music.REQUEST_STATES, r.get("state"))
                if state not in music.OPEN_REQUESTS:
                    if state in settled:
                        settled[state] += 1
                    continue
                open_.append(self._request_entry(r, measure))
            result["requests"] = open_
            result["requests_settled"] = settled

        if raw.get("goals") is not None:
            # A goal the show has used is up unless it is done; the mod's active mark is
            # null when it cannot read FUSER's map of active goals
            goals, done = [], 0
            titles = self._titles(raw["goals"])
            for g in raw["goals"]:
                if g.get("completed") is True or g.get("active") is False:
                    done += g.get("completed") is True
                    continue
                entry = {"goal": self._goal_text(g, titles),
                         "done": g.get("performed"), "of": g.get("required")}
                if g.get("bars"):
                    entry["bars"], entry["bars_done"] = g["bars"], g.get("completed_bars")
                goals.append({k: v for k, v in entry.items() if v is not None})
            result["goals"] = goals
            result["goals_done"] = done

        show = raw.get("show") or {}
        if show.get("has_meter") is not None:
            result["crowd_meter_on"] = show["has_meter"]
        # Before the meter is on, its widget holds its starting values, which read like a
        # meter already in play
        crowd = (raw.get("crowd") or {}) if show.get("has_meter") is not False else {}
        if _meter(raw) is not None:
            result["crowd_meter"] = _meter(raw)
        band = music._named(music.CROWD_STATES, crowd.get("CurHealthState"))
        if band:
            result["crowd"] = band
        if crowd.get("WarningCount"):
            result["crowd_warnings"] = crowd["WarningCount"]
        fresh = raw.get("freshness") or {}
        low, high, value = fresh.get("min"), fresh.get("max"), fresh.get("value")
        if all(isinstance(v, (int, float)) for v in (low, high, value)) and high > low:
            result["freshness"] = round((value - low) / (high - low) * 100)
        if show.get("score") is not None:
            result["score"] = show["score"]
        results = show.get("results") or {}
        if isinstance(results.get("stars"), (int, float)):
            result["stars"] = round(results["stars"], 2)
        points = results.get("points") or {}
        named = {name: points.get(field) for name, field in POINTS.items()}
        if any(isinstance(v, int) for v in named.values()):
            result["points"] = {k: v for k, v in named.items() if isinstance(v, int)}
        if show.get("ending") is True:
            result["ended"] = True
        if raw.get("problems"):
            # Said rather than hidden, so a part FUSER moved shows up as missing, not empty
            result["unreadable"] = raw["problems"]
        return result

    def _tool_search_songs(self, args: dict) -> dict:
        words = str(args.get("query") or "").casefold().split()
        genre = args.get("genre")
        key = music.key_name(*music.parse_key(args["key"])) if args.get("key") else None
        low, high = args.get("bpm_min"), args.get("bpm_max")
        y_low, y_high = args.get("year_min"), args.get("year_max")
        instrument, disc_type = args.get("instrument"), args.get("disc_type")
        e_low, e_high = args.get("energy_min"), args.get("energy_max")
        tags = store.tags()
        songs = self._library()

        near = None
        if args.get("near"):
            near = next((s for s in songs if s["id"] == args["near"]), None)
            if near is None:
                raise ValueError(f"no song with id {args['near']}")

        found = []
        for song in songs:
            if not song["owned"]:
                continue
            text = f"{song['artist'] or ''} {song['title'] or ''}".casefold()
            if not all(word in text for word in words):
                continue
            if genre and song["genre"] != genre:
                continue
            if key and song["key"] != key:
                continue
            bpm = song["bpm"]
            if (low is not None or high is not None) and not bpm:
                continue
            if (low is not None and bpm < low) or (high is not None and bpm > high):
                continue
            year = song["year"]
            if (y_low is not None or y_high is not None) and not year:
                continue
            if (y_low is not None and year < y_low) or (y_high is not None and year > y_high):
                continue
            if instrument or disc_type:
                # The discs that fill a request or a goal for these, and only them, so the
                # answer is the disc to drop rather than a song to look through
                discs = [d for d in song["discs"]
                         if (not instrument or d["instrument"] == instrument)
                         and (not disc_type or d["type"] == disc_type)]
                if not discs:
                    continue
                song = {**song, "discs": discs}
            tag = tags.get(song["id"], {})
            if args.get("untagged") and tag:
                continue
            energy = tag.get("energy")
            if (e_low is not None or e_high is not None) and energy is None:
                continue
            if (e_low is not None and energy < e_low) or (e_high is not None and energy > e_high):
                continue
            if near and (song["id"] == near["id"] or not _near(near, song)):
                continue
            found.append({**song, **({"tags": tag} if tag else {})})

        found = [{k: v for k, v in s.items() if k != "owned"} for s in found]
        offset = max(0, int(args.get("offset") or 0))
        limit = max(1, int(args.get("limit") or 25))
        return {"total": len(found), "songs": found[offset:offset + limit]}

    def _tool_tag_songs(self, args: dict) -> dict:
        self._library()
        known = {s["id"] for s in self._songs}
        tagged, unknown = [], []
        for entry in args["songs"]:
            song_id = entry.get("id")
            if song_id not in known:
                unknown.append(song_id)
                continue
            energy = entry.get("energy")
            if energy is not None and (not isinstance(energy, int) or not 1 <= energy <= 10):
                raise ValueError(f"{song_id}: energy must be 1 to 10")
            store.tag(song_id, entry, clear=bool(entry.get("clear")))
            tagged.append(song_id)
        result = {"tagged": len(tagged)}
        if unknown:
            result["unknown_ids"] = unknown
        return result

    def _tool_plan_mix(self, args: dict) -> dict:
        _, plan = self._plan(args)
        return plan.report(args.get("from_bar"), args.get("to_bar"))

    def _tool_save_mix(self, args: dict) -> dict:
        name = str(args["name"]).strip()
        if not name:
            raise ValueError("a mix needs a name")
        _, plan = self._plan(args)
        fields = {k: args[k] for k in ("key", "bpm", "steps", "end_bar") if k in args}
        report = plan.report(0, 0)
        store.save_mix(name, fields, self.client)
        return {"saved": name, "bars": report["bars"], "length": report["length"],
                "checks": len(report["checks"])}

    def _tool_load_mix(self, args: dict) -> dict:
        mix = store.load_mix(args["name"])
        return {k: mix[k] for k in ("name", "saved", "key", "bpm", "steps", "end_bar") if k in mix}

    def _tool_list_mixes(self, args: dict) -> dict:
        return {"mixes": store.list_mixes()}

    def _tool_play_mix(self, args: dict) -> dict:
        name, plan = self._plan(args)
        schedule = plan.schedule()
        opening = {a["deck"] for op, a in (schedule[0][1] if schedule else []) if op == "drop"}
        self.bridge.call("ping")  # a closed game fails here, not inside the player
        report = plan.report(0, 0)
        self.player.start(name or "unsaved plan", schedule, plan.end, opening)
        return {"playing": name or "unsaved plan", "bars": report["bars"],
                "length": report["length"]}

    def _tool_stop_mix(self, args: dict) -> dict:
        self.player.stop()
        return {"stopped": True}

    def _tool_keep_alive(self, args: dict) -> dict:
        if args.get("stop"):
            self.player.stop_keep_alive()
            return {"keep_alive": False}
        entries = args.get("discs") or []
        self._library()
        pool = []
        for entry in entries:
            disc = str(entry.get("disc"))
            known = self._discs.get(disc)
            if known is None:
                raise ValueError(f"FUSER has no disc named {disc}, search_songs lists them")
            if known["owned"] is False:
                raise ValueError(f"{disc} is from {known['song']}, DLC you do not own")
            pool.append((disc, _deck(entry)))
        # One fewer and every turn could find the rest on the decks or remembered
        if len({disc for disc, _ in pool}) <= REMEMBERED:
            raise ValueError(f"keep_alive needs at least {REMEMBERED + 1} different discs, so "
                             f"one is always fresh to the crowd")
        self.bridge.call("ping")
        self.player.keep_alive(pool)
        return {"keep_alive": True, "discs": len(pool)}

    def _tool_drop_disc(self, args: dict) -> dict:
        disc = str(args["disc"])
        self._library()
        known = self._discs.get(disc)
        if known and known["owned"] is False:
            raise GameError(f"{disc} is from {known['song']}, DLC you do not own, and would "
                            f"play silence. search_songs lists only songs you own")
        try:
            drop = {"name": disc, "deck": _deck(args), "queue": bool(args.get("queue"))}
            if drop["queue"]:
                self.bridge.call("drop", **drop)
            elif args.get("on_pickup"):
                pickups = (known or {}).get("pickups")
                loop = (known or {}).get("loop_beats")
                if not pickups or not loop:
                    # Older custom songs were made without them
                    raise GameError(f"{disc} has no pickups to drop on; drop it on the downbeat")
                # Queued first, so the disc is on the deck and loaded when the pickup comes,
                # as one in the player's hand is: spawned at the pickup itself, FUSER can
                # only bring it in at the next bar
                self.bridge.call("drop", **{**drop, "queue": True})
                target = self.player.on_pickup([("drop_queue", {})], pickups, loop)
                aimed = round(target % loop, 2)
            else:
                # Timed as play_mix times its bars, so the campaign counts it on the downbeat
                self.player.on_downbeat([("drop", drop)])
        except GameError as e:
            if "no disc named" in str(e):
                raise GameError(f"FUSER has no disc named {disc}, search_songs lists them") from e
            raise
        result = {"deck": args["deck"], "disc": self._describe(disc),
                  "queued": bool(args.get("queue"))}
        if args.get("on_pickup") and not args.get("queue"):
            # The beat in the disc's loop it was aimed at, to tell one pickup from another
            result["pickup_beat"] = aimed
        return result

    def _tool_drop_queue(self, args: dict) -> dict:
        self.player.on_downbeat([("drop_queue", {})])
        return {"dropped": True}

    def _tool_eject(self, args: dict) -> dict:
        # On the downbeat, so the disc stops at the end of a bar rather than partway in
        self.player.on_downbeat([("eject", {"deck": _deck(args)})])
        return {"deck": args["deck"], "ejected": True}

    def _tool_set_key(self, args: dict) -> dict:
        key, mode = music.parse_key(str(args["key"]))
        # On the downbeat, as drops are: the campaign's crowd favors a change made on the
        # beat, though whether a key or tempo goal checks the timing is not known
        self.player.on_downbeat([("set_key", {"key": key, "mode": mode})])
        return {"key": music.key_name(key, mode)}

    def _tool_set_tempo(self, args: dict) -> dict:
        bpm = int(args["bpm"])
        self.player.on_downbeat([("set_tempo", {"bpm": bpm})])
        return {"bpm": bpm}

    def _tool_mute_deck(self, args: dict) -> dict:
        self.player.on_downbeat([("mute", {"deck": _deck(args), "on": bool(args["mute"])})])
        return {"deck": args["deck"], "muted": bool(args["mute"])}

    def _tool_solo_deck(self, args: dict) -> dict:
        self.player.on_downbeat([("solo", {"deck": _deck(args), "on": bool(args["solo"])})])
        return {"deck": args["deck"], "soloed": bool(args["solo"])}


def _meter(raw: dict) -> int | None:
    """The crowd meter from 0 to 100, or None before it is on. Until then its widget holds
    its starting values, which read like a meter already in play."""
    if (raw.get("show") or {}).get("has_meter") is False:
        return None
    crowd = raw.get("crowd") or {}
    health, most = crowd.get("CurHealth"), crowd.get("MaxHealth")
    if isinstance(health, (int, float)) and isinstance(most, (int, float)) and most > 0:
        return round(health / most * 100)
    return None


def _near(a: dict, b: dict) -> bool:
    """Whether FUSER would shift and stretch song b little to play it beside song a."""
    if not a["bpm"] or not b["bpm"] or not a["key"] or not b["key"]:
        return False
    stretch = music.stretch_percent(b["bpm"], a["bpm"])
    if stretch is None or abs(stretch) > music.STRETCH_PERCENT:
        return False
    return music.key_relation(music.parse_key(a["key"]), music.parse_key(b["key"])) != "clash"


def _deck(args: dict) -> int:
    deck = args.get("deck")
    if not isinstance(deck, int) or isinstance(deck, bool) or not 1 <= deck <= 4:
        raise ValueError(f"deck must be 1 to 4, got {deck!r}")
    return deck - 1
