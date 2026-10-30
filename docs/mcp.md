# MCP server
Crystalize runs as a Model Context Protocol server with `crystalize mcp`, so an agent can plan mixes and play them in FUSER. It plans the way pink diamond does: the agent writes the whole mix first, Crystalize checks it for what the agent cannot hear, and only then does it play.

## Setup
Point an MCP client at `crystalize mcp`. The server speaks MCP over stdin and stdout and needs nothing installed beyond Crystalize and the mod.

```sh
claude mcp add crystalize -- uv run --directory /path/to/Crystalize crystalize mcp
```

Playing needs FUSER running with a freestyle session or a campaign set open. Searching, tagging and planning work while it is closed, from the last library Crystalize saw.

## Decks and discs
FUSER has four decks, numbered 1 to 4 here. Each plays one disc, a single part of a song:

| Type | What |
| --- | --- |
| `beat` | Drums and percussion |
| `bass` | The bassline |
| `loop` | A riff or hook |
| `lead` | The vocal, usually, or a lead instrument |

FUSER conforms every disc to the session's key and tempo and brings it in on the beat. That makes any two discs play together, not sound good together, which is what the checks are for. Keys are written `Ab` for major and `Abm` for minor, as bejeweled writes them.

## Prompts
Crystalize offers three prompts, which clients such as Claude Code show as slash commands. Each sets out the order to use the tools in, and costs nothing until chosen.

| Prompt | What |
| --- | --- |
| `tag_library` | Tags the songs the agent knows, 50 at a time, in a session that does nothing else |
| `build_mix` | Chooses songs, plans a mix, fixes what its checks find, then saves and plays it. Takes an optional `brief`, `minutes` and `name` |
| `play_set` | Plays the campaign set FUSER has open, answering goals and requests through `await_event`, and says the stars at the end |
| `review_mix` | Loads a saved mix, fixes its weak parts, and saves it again. Takes the mix's `name` |

## Tools
| Tool | What |
| --- | --- |
| `get_state` | The key, the tempo, the tempo range FUSER allows, each deck's playing and queued disc, and the mix playing, with the bar it is on |
| `get_set` | In a campaign set, the open crowd requests, the goals not yet done, the crowd meter and the score, see Campaign |
| `await_event` | In a campaign set, waits until a request, goal or the meter changes and says what, see Campaign |
| `search_songs` | Songs whose title or artist has every word of `query`, narrowed by `genre`, `key`, `bpm_min`, `bpm_max`, `year_min`, `year_max`, `instrument`, `disc_type`, `energy_min`, `energy_max`, `untagged` and `near`, a page at a time. Each lists its tags and its discs by name |
| `tag_songs` | Writes down what songs are like, see Tags |
| `plan_mix` | Checks a plan or a saved mix without playing it, see Plans |
| `save_mix`, `load_mix`, `list_mixes` | Saves a plan under a name, gives it back to change, and lists them |
| `play_mix` | Plays a plan or a saved mix, each step on its bar |
| `stop_mix` | Stops the mix. The decks keep playing what they have |
| `keep_alive` | Swaps discs from a list now and then, so a campaign set's crowd meter does not drain while the agent thinks |
| `go_to` | Takes FUSER to a campaign set, freestyle or the main menu without its menus, see Launching |
| `drop_disc`, `drop_queue`, `eject`, `set_key`, `set_tempo`, `mute_deck`, `solo_deck`, `take_snapshot` | Act outside a plan, timed to the next downbeat as `play_mix` times a bar. A snapshot is kept in the player's profile, as one taken with the key is. A disc dropped on a deck Crystalize muted unmutes it on the same downbeat, since FUSER rewards nothing dropped on a muted deck |

A call with an argument its tool does not take is refused, and the error lists the arguments it does take.

`near` takes a song id and keeps the songs in the same, the relative or a neighbouring key, within 8% of its tempo or of half or double it. Those are the songs FUSER shifts and stretches least to play beside it. It narrows the library and does not choose: most of what it returns will not belong.

With `instrument` or `disc_type`, `search_songs` lists only the discs that match, which are the ones a crowd request or goal asking for them takes. Instruments are FUSER's own names, matched exactly.

`search_songs` leaves out DLC you do not own, which FUSER loads and plays as silence, and `drop_disc` and plans refuse it.

## Tags
The library says nothing about how a song sounds, and key and tempo alone will put a ballad's vocal over a drill beat. Tags are where an agent writes that down, once, for every later session.

| Tag | What |
| --- | --- |
| `energy` | How hard the song hits, from 1, a still ballad, to 10, the hardest in the library |
| `style` | Its style in a few words |
| `lyrics` | What the words are about and how they feel |
| `mood_clash` | The sound and the words pull opposite ways |

Tag in a session that does nothing else, with the `tag_library` prompt. An agent tags from what it knows of a recording, so a song it does not know is left untagged. Crystalize keeps tags in `~/.local/share/crystalize/tags.json`, by FUSER's song id.

## Plans
A plan is the opening key and tempo and a list of steps, each on a bar counted from 0, the bar the opening discs land on:

```json
{"key": "Abm", "bpm": 124, "end_bar": 64, "steps": [
  {"bar": 0, "do": "drop", "deck": 1, "disc": "badguybt"},
  {"bar": 8, "do": "drop", "deck": 2, "disc": "mrbrightsidebs"},
  {"bar": 32, "do": "mute", "deck": 1},
  {"bar": 40, "do": "unmute", "deck": 1},
  {"bar": 48, "do": "key", "key": "Ebm"}
]}
```

`do` is `drop`, `eject`, `mute`, `unmute`, `key` or `tempo`. Steps on one bar happen together. The mix ends at `end_bar`, 16 bars after the last step unless the plan says.

`plan_mix` refuses a plan that cannot play, with every problem listed: a disc that does not exist or is not owned, an empty deck ejected, a tempo FUSER does not allow. A plan that can play comes back as a timeline of segments, the stretches where nothing changes. Each has its bars, the time it starts, the key, tempo and energy, and what each deck plays, with the pitch shift and stretch FUSER gives it. Pass `from_bar` and `to_bar` to list part of a long mix; the checks over the whole mix are always there.

## Checks
| Check | What |
| --- | --- |
| `two vocals` | Two lead vocals at once. A lead played by an instrument does not count |
| `no beat`, `no bass` | Nothing in that part. Fine for a breakdown, and a problem anywhere else |
| `two basses` | Two basslines fight for the same space |
| `one song` | Three or more decks from one song, which is the original and not a mix |
| `pitched far` | FUSER shifts the disc further than it takes unnoticed, from the disc's own table for the session key. Up thins a part sooner than down darkens it, so a loop or instrumental lead is flagged past 3 semitones up or 5 down, a vocal past 3 up or 4 down, and a bassline, which starts low, past 5 either way |
| `no major version`, `no minor version` | The disc has no version in the session's mode |
| `stretched too far` | More than 8% off the disc's own tempo, or 16% above 140 BPM, allowing for half and double time |
| `silence` | Nothing is playing |
| off the phrase | A step not on a multiple of 4 bars |
| `nothing changes` | More than 32 bars with no step |
| dropped twice | The same disc dropped more than once |
| key clash | A key change that is not to the relative key or a neighbour |
| tempo jump | A tempo change of more than 8% |
| energy jump | Energy moving by 3 or more between segments, from the tags |

A check is a warning, not a rule. The agent should fix it or know why it is keeping it.

## Playing
`play_mix` returns at once and plays in the background, sending each bar's steps together, timed to reach FUSER just after the bar's downbeat. FUSER plays them from the downbeat, and in a campaign set a drop timed this way counts as a drop on the downbeat. Set FUSER to quantize to cues and a drop waits for the track's next cue instead, which no plan can know, and custom songs often have none.

When the decks are empty, bar 0 starts at once. When something is playing, bar 0 waits for the next downbeat, so the mix starts in phase with it. Decks the plan does not fill at bar 0 are emptied first, since the plan was checked as if they were. A deck already playing the disc bar 0 gives it keeps it rather than having it dropped again, and a key or tempo the session already has is not set again. `get_state` shows the bar the mix is on. A disc dropped by hand during a mix stays until a step replaces it.

## Campaign
Crystalize plays campaign sets as it plays freestyle, and any disc in the library can be dropped, not only the set's crate. Drops, ejects and the rest count toward goals and crowd requests as if you had made them, and a drop `play_mix`, `drop_disc` or `drop_queue` makes counts as one on the downbeat. To change one deck, use `drop_disc`: it waits for the next downbeat, up to a bar, and returns once the drop is sent. A plan that repeats the other decks' discs at bar 0 keeps them, so the crowd does not count them as repeats.

The crowd meter drains after 6 idle seconds, calls more than 3 actions in 24 beats overactive, and counts a drop of one of the last 3 discs as a repeat. `keep_alive` works inside those rules. 3 bars after the last change, made by anyone, it drops a disc of its list on that disc's deck, choosing one that makes a mix not heard this session and then the one dropped least, skipping one already on a deck or among the last 3 dropped, and skipping the bar if a drop would make the set overactive. It waits while a mix plays, and while a crowd request is open, since a disc it dropped could fill the request by chance and spend the disc a combo needed. `get_state` shows what it last dropped. It rides out a command FUSER leaves unanswered and stops after 3 in a row. Give it at least 4 discs that can swap in for each other, such as beats on one deck and loops on another, and stop it with `stop`.

Freshness, the mix's newness, rises when a disc is played, a mute changes or the number of playing decks changes, and most when the discs together make a mix not heard before. It drains once 16 beats pass with no change. Later sets lean on it, so give `keep_alive` 12 or more discs: a few cycled over two decks soon make only mixes already heard. With `fill`, Crystalize chooses up to 8 discs of a type for each deck named, from owned songs whose key and tempo fit the session, the least stretched first, and only of a genre when one is given; the reply lists what it chose. With `bars`, it changes a disc that many bars after the last change instead of 3, and no longer holds back for the overactive rule. A change every bar holds freshness above 80.

`drop_disc` with `on_pickup` lands a disc on its next pickup, where a vocal or a fill comes in ahead of the bar, for goals that ask for a drop on a pickup. Crystalize queues the disc, so it is loaded, then drops it just after the pickup, and says which beat of the disc's loop it aimed at. Some older custom songs have no pickups, and `drop_disc` refuses those.

`get_set` reports the set as FUSER keeps it:

| Field | What |
| --- | --- |
| `measure` | The measure FUSER is on. Requests are timed in these, not in a mix's bars |
| `requests` | Each scheduled or active crowd request: what it `wants`, such as "a disc played on Synth" or "a disc from Bad Girls by M.I.A.", the artist given since two songs can share a title, and the measure it `starts` and `ends` on. An active one has `measures_left`. FUSER chooses what a request wants only when it starts |
| `requests_settled` | How many have passed and failed |
| `goals` | Each goal not yet done, with how many times it has been `done` `of` the times it needs |
| `goals_done` | How many are done |
| `goals_missed` | Each goal that ended undone |
| `crowd_meter_on` | Whether this part of the set has a crowd meter. The early ones do not |
| `crowd_meter` | From 0 to 100, starting at 50. The set fails at 0 unless No Fail is on |
| `crowd` | The meter's band: low, medium, high or peak |
| `crowd_warnings` | Warnings the crowd has given, such as for changing too much at once |
| `crowd_band_bonus` | The points a section ends on for each band, which differ between sets |
| `freshness` | From 0 to 100, how new the mix sounds. A drop raises it and it fades while nothing changes |
| `freshness_zone`, `in_freshness_zone` | The range of freshness the set pays for staying in, and whether it is there. Above it, freshness bonuses shrink |
| `crowd_actions`, `overactive` | The actions the crowd counts in its last 6 bars, and whether that is more than it allows |
| `meter_paused`, `failure_pending` | Present when the meter has stopped draining or is about to fail the set |
| `show_state` | Whether the show has started, the section is looping, a show break is replaying and the section has goals |
| `score` | The set's score so far |
| `stars` | Stars earned so far, out of 5 |
| `points` | Where the score came from: requests, request combos, goals, perfect, early, late and bad timing, and the crowd meter |
| `ended` | Present once FUSER has counted the set out. The set still reads on the results screen |
| `unreadable` | Parts the mod could not read, if any, with why |

The meter drains about 1 a second once 6 seconds pass without an action, and a good action adds 3. A bad one takes 4, and so do a missed request and a missed goal. FUSER remembers the last 3 discs and counts more than 3 actions in 6 bars as too much, so keep it fed with one fresh disc every few bars rather than many at once.

A goal's text keeps FUSER's placeholders where the mod cannot fill them yet, such as "{Transition} Smart Riser". Disc types, instruments, genres and the song a goal features are filled in, so "Drop a {btType}" reads "Drop a beat (blue)", "Drop {lpGuitar} from {RequiredSong}" reads "Drop guitar loop (yellow) from Rock the Casbah by The Clash" and "Include 2+ {Genre} discs" reads "Include 2+ Rock discs".

`await_event` waits for the set to change, so the agent does not spend its turns polling `get_set`. It returns the events and `get_set`'s view together. Crystalize compares each read with the last one the agent saw through either tool, so anything that happened while the agent was busy comes back at once. Otherwise it waits up to `seconds`, 20 by default and at most 60.

| Event | When |
| --- | --- |
| `request_coming` | A request is scheduled. What it wants is chosen when it starts |
| `request_started` | A request starts, with what it wants and the measure it ends on |
| `request_ending` | An active request has 4 measures or fewer left, said once |
| `request_passed`, `request_failed` | A request settles |
| `goal_started` | A new goal is up |
| `goal_done` | A goal is done |
| `goal_ended` | A goal stopped before it was done |
| `meter_on` | The crowd meter comes on |
| `meter_low` | The meter falls below `meter_below`, 30 by default, said once on the way down |
| `set_ended` | FUSER counts the set out, or it can no longer be read, with the stars earned and the goals that ended undone |
| `keep_alive_stopped` | `keep_alive` gave up, with why |

## Launching
`go_to` and `crystalize launch` take FUSER where it is sent without its menus: from the intro, the main menu, or inside a set, which starts it over. Crystalize signs in first if FUSER has just started, since that is what loads the save, then loads the venue and walks the set into play, past the promoter's intro. A set is its venue, 1 to 6, and its number there as the game lists them, and `crystalize launch --list` lists them all. Its crate gets the discs the set gives, as the loadout screen would.

Nothing checks that a set is unlocked, and finishing one saves the result like any other, so back up `FuserSave` in Steam's `userdata` folder for FUSER before playing ahead. A set reached this way ends on a rewards screen that never moves on. The result is saved by then, and `go_to` menu or the next set leaves it.

```sh
uv run crystalize launch --set 3.1
uv run crystalize launch --freestyle --restart
```

`--restart` installs the mod, closes FUSER and starts it again through Steam, for trying a change to the mod. `launch` talks to the game with a bridge of its own, so run it while the MCP server is idle.

## Tempo range
FUSER allows 90 to 157 BPM. FuserSongLoader builds from May 2026 on widen it to 60 to 200. Crystalize reads the range from the game, so plans are checked against whichever you have.

## Saved mixes
`save_mix` refuses a plan `plan_mix` would refuse, and keeps the rest in `~/.local/share/crystalize/mixes/`, stamped with the time and the MCP client's name. Saving under a name again replaces the mix.

## Set log
Crystalize keeps a record of each campaign set in `~/.local/share/crystalize/sets/`, one file a set named for when it began, one JSON object a line. It holds `get_set`'s view whenever it changed and every event, each command sent to FUSER, and the time of each. It begins at the first read of a set in play and ends at `set_ended`, so a run that lost points can be gone over measure by measure.

## Debugging
`crystalize mcp --debug` adds two tools for trying FUSER's functions without a mod change and a restart for each guess. Leave it off otherwise: `debug_lua` can call anything in the game, including what crashes it.

| Tool | What |
| --- | --- |
| `debug_read` | Named fields of the first object of a class, or of every one with `all`, without calling anything |
| `debug_lua` | Runs Lua on FUSER's game thread with UE4SS's globals and the mod's helpers, and returns what it returns and prints |

`crystalize lua '<code>'` does what `debug_lua` does from the command line, or reads the code from stdin with `-`, for when the server is not running with `--debug` or is running code older than the working tree. It has a bridge of its own, so use it while the server is idle.

Run only one server against the game at a time, so switch the flag on the one you have rather than adding a second:

```sh
claude mcp add crystalize -- uv run --directory /path/to/Crystalize crystalize mcp --debug
```

## Log
Crystalize writes a line to stderr for every call: the tool, the size of its reply in bytes and how long it took. A reply stays in the agent's context for the rest of the session, so this is where to look when a session costs more than expected.
