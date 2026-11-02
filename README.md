# CleanCut

CleanCut makes a family-friendly copy of a movie, ready for Jellyfin. You give it a movie, tick what you
want filtered, and it saves a new copy next to the original. It **covers** things instead of skipping
them, so you still follow the story.

- **Nudity:** covered with a big black box (or a heavy blur), or the whole screen goes black.
- **Sex scenes:** cut out if nobody is talking. If people are talking, the screen goes black and you still hear them.
- **Sexually suggestive:** strippers, underwear, intense or erotic kissing. The screen goes black or a box covers it.
- **Language:** F-word, other strong swears, mild words (d**n, h**l), taking God's name in vain, and slurs,
  each with its own checkbox. Words are muted (or bleeped, if you like). On surround sound (5.1, 7.1) only the
  voices are muted, so the music and sound effects keep playing. Subtitles show them as `d**n`.
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

CleanCut can only see the movie folder you set (and the folders inside it), and saves there too.
If you'd rather keep filtered movies in a separate folder, add a line like
`OUTPUT_DIR=C:/Users/YourName/Filtered Movies` to the settings file (`.env`) and start CleanCut again.
It then saves there by default, and you can still pick any folder inside either one each time.

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
2. Drag a movie onto the page, or click *pick one from your media folder*.
3. Tick what to filter and choose where to save.
4. **When to start:** it starts at **2:00 AM** unless you change it. Pick any other time, or tick
   **Start right away**. It goes back to 2:00 AM every time you open the page.
5. Click **Start**. A full movie usually takes a few hours (see below). You can close the browser;
   it keeps going (or keeps waiting for its start time). While it waits you can still click
   **Start now instead** or **Cancel**.
6. Review is off unless you tick it, so the movie is saved by morning without you. If you ticked
   *review*, look over the blurred pictures, change anything you want, and click **Save filtered movie**.

### The new file and Jellyfin

The new file gets the original's full name plus ` - Filtered`, for example
`Deadpool (2016) [imdbid-tt1431045] - [Bluray-1080p][EAC3 5.1][x264]-hallowed - Filtered.mkv`.
The original is never renamed or changed.

Saved **in the same folder as the original**, Jellyfin shows the two as one movie with two versions:
the original stays the one that plays by default, and the copy appears in the version menu as **Filtered**.
(Tested on Jellyfin 12.4.) This works when each movie has its own folder and the file name starts with
the folder name, followed by ` - ` or nothing, which is how Radarr names files by default:

| Works | Shows up as a second movie instead |
|---|---|
| `Up (2009)/Up (2009).mkv` | `Cars (2006)/Cars (2006) [Bluray-1080p].mkv` (no ` - ` after the folder name) |
| `Inception (2010)/Inception (2010) - WEBDL.mkv` | `All Movies/Toy Story (1995) - Bluray-1080p.mkv` (no folder of its own) |
| `Deadpool (2016)/Deadpool (2016) [imdbid-tt1431045] - [Bluray-1080p]....mkv` | |

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
  worked on, and lets it sleep normally the rest of the time (the screen can still turn off). This is set
  up the first time you double-click `Start CleanCut` and then runs hidden every time you sign in.
  Two things it can't stop: **closing a laptop's lid** (leave it open, or set *"When I close the lid"* to
  *Do nothing* while plugged in, in Control Panel → Power Options) and running out of battery (plug it in).
  To turn it off, delete *CleanCut keep-awake* from your Startup folder (press Win+R, type `shell:startup`).
- **Linux:** turn off automatic suspend in your desktop's power settings. Servers usually don't sleep anyway.

## Stopping and updating

- To stop CleanCut: on Windows, double-click **`Stop CleanCut`** (or quit Docker Desktop);
  on Linux, run `bash stop-cleancut.sh`.
- To update: download the new ZIP, unzip it over the old folder (keep your `.env` file, which holds
  your settings), then start CleanCut again the usual way. With git on Linux: `git pull`, then
  `bash start-cleancut.sh`.

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

Built on ideas from PureFrame (MIT), monkeyplug/cleanvid (BSD) and Cens-arr (MIT).
