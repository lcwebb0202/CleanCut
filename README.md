<h1>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="logo/cleancut-logo-white.svg">
    <img src="logo/cleancut-logo-dark.svg" alt="CleanCut" height="72">
  </picture>
</h1>

> **Logo for a dashboard or homepage:**
> [icon (PNG)](logo/cleancut-icon.png) · [icon (SVG)](logo/cleancut-icon.svg) · [all logo files](logo).
> Direct link to paste into a dashboard: `https://raw.githubusercontent.com/lcwebb0202/CleanCut/main/logo/cleancut-icon.png`
> (white C, see-through background; `cleancut-icon-dark.png` is the dark one for light backgrounds).

**An AI video filter that makes a family-friendly copy of any video file: it mutes swearing and covers
nudity, and runs entirely on your own computer.**

You give it a video, tick what you want filtered, and it saves a new copy next to the original, ready to
watch or to add to a media server like Jellyfin. It **covers** things instead of skipping them, so you
still follow the story. It works on any video file you have the right to use: family videos, your own
YouTube or class projects, clips for a church or school group, or movies in your own library.

> **Built with AI.** CleanCut was built almost entirely by an AI coding assistant
> ([Claude Code](https://claude.com/claude-code)), directed, tested and reviewed by me. It's a personal
> project, shared as is: **no guarantees, no warranty**. It can miss things or make mistakes, and I'm
> not responsible for how it's used or for any problems it causes. Only use it on videos you have the
> right to use, and check anything that really matters yourself.

### How it works, in short

- **Speech:** [faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes the dialogue word
  by word. When the video has text subtitles, every swear in them is checked against what was heard, and
  missed lines are listened to again. On one test film, speech recognition alone caught 80 of 138
  subtitled swears; with the subtitle check it caught all of them.
- **Voice-only muting:** on surround sound, a swear is removed by cancelling just the dialogue channel,
  so music and sound effects keep playing. A per-word test falls back to muting every speaker when the
  voice (or its room echo) is heard on other speakers too.
- **Pictures:** [NudeNet](https://github.com/notAI-tech/NudeNet) finds exposed body parts, and
  [CLIP](https://github.com/openai/CLIP) with the [LAION safety classifier](https://github.com/LAION-AI/CLIP-based-NSFW-Detector)
  recognizes sexual or suggestive scenes. Covers start at the camera cut, before anything shows,
  because early and too big beats late and too small. Redesigning these rules took one test film from
  380 flagged moments (mostly false alarms) to 24, with every real scene still caught.
- **Review without seeing it:** the optional review screen shows heavily blurred previews with
  everything detected painted black.
- **The app:** a FastAPI web app in Docker with a queue for overnight runs, NVIDIA GPU acceleration
  when there is one (it also runs on a plain laptop, just slower), and one-click updates.

- **Nudity:** covered with a big black box (or a heavy blur), or the whole screen goes black.
- **Sex scenes:** cut out if nobody is talking. If people are talking, the screen goes black and you still hear them.
- **Sexually suggestive:** strippers, underwear, intense or erotic kissing. The screen goes black or a box covers it.
- **Language:** F-word, other strong swears, mild words (d**n, h**l), taking God's name in vain, and slurs,
  each with its own checkbox (mild words and slurs start unticked; tick them if you want them too).
  Words are muted (or bleeped, if you like). On surround sound (5.1, 7.1) only the
  voices are muted, so the music and sound effects keep playing. Subtitles show them as `d**n`.
  The filtered copy has two audio tracks: **English - Filtered**, which plays by default, and the untouched
  **English - Original**, which you can pick in the player's audio menu like another language. Audio in
  other languages is kept too, labeled *(not filtered)*: CleanCut only listens for English swears.
  When a scene is cut out, it's cut from every audio track, so they all stay in sync.
  If the movie has text subtitles, CleanCut double-checks against them, which catches lines shouted
  over loud action that speech recognition misses.
- **Review (optional):** before saving, see everything it found as **blurred** pictures, with anything
  it detected painted black, so you can check them without seeing anything graphic. Each moment has a few
  pictures, starting with the clearest one. Moments it's sure about are listed apart from the "possible"
  ones, and letting a sure one play asks you first.

The original movie is never changed. No movie ever leaves your computer.

> It's an AI, so it isn't perfect. It works best on clear, well-lit scenes and can miss things in
> very dark ones. Use the review screen, and watch a new movie yourself first if it really matters.

## What you need

- A **Windows 10/11** or **Linux** PC with **about 25 GB of free space** for CleanCut, plus space for the filtered movies.
- **Docker**: Docker Desktop on Windows, Docker Engine on Linux (both free; see the steps below).
- An NVIDIA graphics card makes it several times faster, but it isn't needed.

## Setting it up on Windows (once)

1. **Install Docker Desktop.** Download it from the link above and run the installer with the
   normal settings. Restart your PC if it asks. Open Docker Desktop once and accept its terms.
2. **Download CleanCut.** On this GitHub page, click the green **Code** button, then **Download ZIP**.
   Unzip it somewhere *outside* OneDrive, for example `C:\CleanCut`.
3. **Double-click `Start CleanCut`** in that folder.
   - The first time, Notepad opens a small settings file. Change the `MEDIA_DIR=` line to your
     movie folder, using forward slashes, for example `MEDIA_DIR=D:/Movies`. Save and close Notepad.
   - If you have an **NVIDIA** graphics card, also remove the `#` at the start of the two
     `COMPOSE_...` lines before saving.
   - The first start downloads about 13 GB and can take 30 minutes or more. Later starts are quick.
4. Your browser opens **http://localhost:8800**. That's CleanCut.

CleanCut can only see the movie folder you set (and the folders inside it). Each filtered movie is
saved in the same folder as its original, so Jellyfin shows it as a version of that movie. You can pick
another folder when adding movies; that only counts for those movies. To be able to pick a folder
outside your movie folder, add a line like `OUTPUT_DIR=C:/Users/YourName/Filtered Movies` to the
settings file (`.env`) and start CleanCut again.

## Setting it up on Linux (once)

1. **Install Docker Engine** (with the Compose plugin) by following the steps for your distribution at
   https://docs.docker.com/engine/install/. The quick way on most distributions is:
   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker $USER
   ```
   Then **log out and back in**, so you can use Docker without `sudo`.
2. **Download CleanCut.** On this GitHub page, click **Code → Download ZIP**, and unzip it, for example
   to `~/CleanCut`. (Or, with git: `git clone https://github.com/lcwebb0202/CleanCut.git ~/CleanCut`.)
3. **Start it.** Open a terminal in that folder and run:
   ```bash
   bash start-cleancut.sh
   ```
   The first time, it asks for your movie folder (for example `/home/yourname/Movies`), and saves
   your settings in a file called `.env`. The movies CleanCut saves belong to your user, as usual.
   The first start downloads about 13 GB and can take 30 minutes or more.
4. Open **http://localhost:8800** in your browser (it tries to open it for you).

CleanCut keeps running in the background and starts again when the computer restarts.
To stop it: `bash stop-cleancut.sh`.

*NVIDIA graphics card on Linux:* install the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
then remove the `#` in front of the `COMPOSE_FILE=` line in `.env` and run `bash start-cleancut.sh` again.

*Running Jellyfin on the same Linux PC?* Point `MEDIA_DIR` at your Jellyfin movie folder, so the
filtered copies land right next to the originals.

## Using it

1. Open **http://localhost:8800**. (On Windows, start **Docker Desktop** first, or just double-click `Start CleanCut`.)
2. Drag movies onto the page (one or several), or click *pick from your media folder*. In the
   picker you can tick several movies, or click **Add all in this folder** for a whole TV season.
3. Tick what to filter. Movies added together get the same settings. Each is saved next to its
   original unless you pick another folder (*Change folder*), which only counts for these movies.
4. **When to start:** it starts at **2:00 AM** unless you change it. Pick any other time, or tick
   **Start right away**. It goes back to 2:00 AM every time you open the page.
5. Click **Start**. You get the list of your movies, each with its own progress bar. They're worked on
   one at a time, from the top; a full movie usually takes a few hours (see below). You can close the
   browser; it keeps going (or keeps waiting for its start time, with a **Start now instead** button).
6. **Pause** freezes the movie being worked on exactly where it is, and **Resume** carries on from
   the same spot. **Cancel** stops it (nothing is saved for it) and the next movie starts.
7. To add more, click **+ New movie** under the list. They join the end of the list, with whatever
   settings you tick for them. Movies still waiting can be moved up or down or taken off the list.
8. Review is off unless you tick it, so movies are saved by morning without you. A movie with review
   ticked stops at **Ready for your review** and the next one carries on. Click **Review**, look over
   the blurred pictures, change anything you want, and click **Save filtered movie**: it's saved as soon
   as nothing else is being worked on.
9. If one movie fails (a damaged file, or a full drive), it's marked with what went wrong and the rest
   carry on. Click **Try again** once it's sorted. CleanCut warns you at the top of the list when the
   movies still to be saved won't fit on the drive.

### The new file and Jellyfin

The new file gets the original's full name plus ` - Filtered`, for example
`My Movie (2020) - 1080p - Filtered.mkv`. If the name ends in ` - Default` (some libraries label the
version that plays by default that way), ` - Filtered` takes its place: `My Movie (2020) - Default.mkv`
becomes `My Movie (2020) - Filtered.mkv`, and Jellyfin shows the versions as *Default* and *Filtered*.
The original is never renamed or changed.

Saved **in the same folder as the original**, Jellyfin shows the two as one movie with two versions:
the original stays the one that plays by default, and the copy appears in the version menu as **Filtered**.
(Tested on Jellyfin 12.4.) This works when each movie has its own folder and the file name starts with
the folder name, followed by ` - ` or nothing, which is how most media managers name files:

| Works | Shows up as a second movie instead |
|---|---|
| `Up (2009)/Up (2009).mkv` | `Cars (2006)/Cars (2006) [Bluray-1080p].mkv` (no ` - ` after the folder name) |
| `Inception (2010)/Inception (2010) - WEBDL.mkv` | `All Movies/Toy Story (1995) - Bluray-1080p.mkv` (no folder of its own) |
| `My Movie (2020)/My Movie (2020) - 1080p.mkv` | |

CleanCut checks this before you start and shows a yellow note if Jellyfin wouldn't group them.
Don't rename the original to `... - Original`: Jellyfin sorts version names alphabetically, so
"Filtered" would then come first and become the default.

### How long it takes

| Computer | 2-hour HD movie | 3-hour 4K movie |
|---|---|---|
| With an NVIDIA graphics card (e.g. RTX 3050 laptop) | about 1.5–2 hours | about 4 hours |
| Without one (processor only) | roughly 6–10 hours | much longer |

### For overnight runs: the PC has to stay awake

If the PC goes to sleep while CleanCut is working, it stops and you have to start that movie again.
If it's asleep at the start time, it starts as soon as the PC wakes up.
- **Windows:** CleanCut keeps the PC awake by itself while a movie is waiting for its start time or being
  worked on, and lets it sleep normally the rest of the time (the screen can still turn off). A small
  hidden helper does this. It's set up the first time you double-click `Start CleanCut` and then runs
  every time you sign in.
  Two things it can't stop: **closing a laptop's lid** (leave it open, or set *"When I close the lid"* to
  *Do nothing* while plugged in, in Control Panel → Power Options) and running out of battery (plug it in).
  To turn it off, delete *CleanCut helper* from your Startup folder (press Win+R, type `shell:startup`).
  That also turns off the update button below.
- **Linux:** turn off automatic suspend in your desktop's power settings. Servers usually don't sleep anyway.

## Stopping and updating

- To stop CleanCut: on Windows, double-click **`Stop CleanCut`** (or quit Docker Desktop);
  on Linux, run `bash stop-cleancut.sh`.
- **Updating on Windows:** CleanCut checks for a new version every day at noon (and when you sign in).
  When there is one, the button in the top right of the CleanCut page changes from a greyed-out
  **Up to date** to **Update available**. To check right away, click the round arrow next to it.
  Click it to see what's new, then click **Update now**. CleanCut restarts with the new version in a
  few minutes and the page refreshes by itself. Your settings (`.env`), the downloaded speech models
  and any review you're in the middle of are kept. It won't update while CleanCut is busy with a
  movie or waiting for its start time.
  `Start CleanCut` also mentions new versions, and double-clicking **`Update CleanCut`** does the same
  update by hand (useful if the button says the helper isn't running).
- **Updating on Linux:** download the new ZIP and unzip it over the old folder (keep your `.env` file),
  or with git, run `git pull`. Then run `bash start-cleancut.sh`.

## If something goes wrong

- *"Docker Desktop isn't running"* (Windows): open Docker Desktop and wait until it says it's running.
- *"Docker isn't running, or your user isn't allowed to use it"* (Linux): run the two commands it
  shows, then log out and back in.
- The page won't open: wait a minute after starting, then refresh. Make sure Docker Desktop is open.
- Anything else: take a screenshot (including the *Details* section on the page) and send it to Luke.

---

### For the technically curious

A Python app in Docker: Whisper (faster-whisper) finds words; NudeNet finds exposed body parts;
CLIP (ViT-L/14) recognizes sex/suggestive scenes; ffmpeg does the rest. The command line also works:
`docker compose run --rm cleancut process "/media/Movie (2020).mkv" --out /media/Filtered`.
Run `docker compose run --rm cleancut --help` for options.

**Logo files** (for a dashboard or homepage) are in [`logo/`](logo): `cleancut-icon` is the C in white
and `cleancut-icon-dark` the C in dark, both on a see-through square, and `cleancut-logo-white` / `-dark`
are the full name (white for dark backgrounds, dark for light ones).
Each comes as SVG (sharp at any size) and PNG. They're all made from one drawing by
`logo/make_logo.py`, which also updates the C in the page header and the browser-tab icon.

Built on ideas from PureFrame (MIT), monkeyplug/cleanvid (BSD) and Cens-arr (MIT). The models it uses
(faster-whisper, NudeNet, CLIP and the LAION classifier) keep their own licenses.

**Disclaimer:** CleanCut is provided "as is", without warranty of any kind. Use it at your own risk, and
only on videos you have the right to use and modify. It was written with the help of AI and tested by
hand, not certified for any purpose.
