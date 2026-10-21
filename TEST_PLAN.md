# SaiHarness manual testing

Mark `[x]` as you go. Each step says what should happen. If the result differs, see "If something is wrong" at the end.

## 0. Preparation

1. Close the app and back up the data (the tests change chats, folders and permissions):

```powershell
Copy-Item data data_backup -Recurse
```

2. Create the test folders `Alpha` and `Beta` (the script can be run again: it restores the files and deletes `.bak`):

```powershell
powershell -ExecutionPolicy Bypass -File scripts\make_test_sandbox.ps1
```

| File | Purpose |
|---|---|
| `Alpha\readme.md` | plain text, contains "PURPLE-MAMMOTH-42" |
| `Alpha\sub\code.py` | a subfolder |
| `Alpha\cp1251.txt`, `Alpha\bom.txt`, `Alpha\crlf.txt` | encodings and line breaks (`cp1251.txt` is Russian on purpose) |
| `Alpha\big.txt` | 750 KB: over the 300 KB limit |
| `Alpha\image.png` | binary |
| `Alpha\.git\config` | a service folder, must not get into the context |
| `Beta\requirements.txt` | "yellow sausage" on line 3, CRLF line breaks |
| `Beta\dup.txt` | "apple and apple" (the fragment occurs twice) |
| `Beta\notes.txt` | a plain note |

3. Start LM Studio, load one model with `tool_use` (for example gemma-4-e4b) and turn on the server on port 1234.
4. Start the app:

```powershell
.venv\Scripts\python app.py
```

---

## A. Startup

- [x] **A1** The window opened: black background, chats on the left, the dialog in the center, tabs on the right: Files, Tools, Web, Memory, Model, Connection, Model Builder.
- [x] **A2** The Tools tab, after 1–3 seconds: `● toolbox — tools: N`. `fs_replace`, `notes_write`, `web_extract` and the other write tools have the `[asks]` tag.
- [x] **A3** The last entry in `data\crash.log` is `=== start ...` (after the app is closed, `=== exit` appears).

## B. Connection (already checked, can be quick)

- [x] **B1** The Connection tab: name `lm`, URL `localhost:1234` → "Check" → `✓ Connected, models on the server: N`. "Save" → the list shows `lm — http://localhost:1234/v1`.
- [x] **B2** URL `localhost:1` → "Check" → `✗ Cannot reach…`. URL `ftp://x` → `✗ Only http and https are supported`. An empty URL → `✗ Empty address`. Saving without a name → `A connection name is required`.
- [x] **B3** "Edit", change the name, "Save": the connection is renamed, profiles keep working (the summary on the Model tab shows the new name).
- [x] **B4** Save a second connection with a name that is already taken → `The name "…" is already taken`.
- [x] **B5** "Delete" asks for confirmation. The profile summary marks this connection `(deleted)`.

## C. Profiles (also quick)

- [x] **C1** Model Builder: name `test`, connection `lm`, "Refresh" → the model list, choose one, System `Answer briefly.`, "Save". On the Model tab the profile is selected, the summary shows the connection, model, temperature, max tokens, system.
- [x] **C2** "Edit" opens the profile in Model Builder, "Delete" asks for confirmation.
- [x] **C3** "Save" without a connection → `Create a connection on the Connection tab first` or `A name, a connection and a base model are required`.
- [ ] **C4 Tool steps.** Model Builder has the "Tool steps" field (16 by default, from 1 to 100). Set `2`, save; the summary on the Model tab shows `Tool steps: 2`. "Edit" brings the value back into the field. Enable tools and give a task that needs more than two calls: after the second round the status shows `Reached the tool step limit (2)`, and the model sums up in text. Set the value you need back.

## D. Chats

- [x] **D1** "+ New chat", send "hello": the chat title became "hello" (the first 40 characters of the first message).
- [x] **D2** Double-click a chat → rename, the title in the center is updated. Right-click → "Delete" with confirmation; after deleting, the neighboring chat is selected.
- [x] **D3** Close and restart the app: chats and history are in place, the most recent one is open. The selected chat is highlighted in yellow.
- [ ] **D4 Switching to a long chat.** Switch to the longest chat (preferably hundreds of messages, with tool calls). The feed is at the bottom, on the last message, at once and until loading finishes, with no jumps to the middle. Scroll up with the wheel right after opening: the app does not pull you back down.

## E. Model answers

- [x] **E1 Locking the model.** After the first message the drop-down list on the Model tab is grey and labeled "The model is locked to this chat…". In a new chat the list is available again.
- [x] **E2 Streaming and reasoning.** "Explain in three sentences what HTTP is". While answering: status `Generating…`, "Send" is grey, "Stop" is active. Reasoning goes in grey above the answer. The "Reasoning" checkbox above the chat hides and shows it. At the end the status is `profile · model`.
- [x] **E3 Scrolling.** "Write a long story about the sea in 15 paragraphs". While the answer streams, scroll up with the wheel: the position holds, no jitter. Scroll down to the end: the window follows the new text again. The wheel works during long reasoning too.
- [x] **E4 Stop.** Press "Stop" in the middle of an answer: the status is `Stopped` at once and a grey line "System: Stopped" appears. The partial answer is saved, you can keep writing.
- [x] **E5 Switching chats during an answer.** Start a long answer and switch to another chat: there "Send" is grey and the status is `A reply is in progress in another chat…`. Go back: the answer keeps streaming and after finishing stays in its own chat.
- [x] **E6 "Request".** The button above the chat: a window with the model, parameters and all messages; the system prompt is visible at the start. After messages with tools the header lists `functions: find_tool, describe_tool, call_tool`.
- [x] **E7 Formatting.** "Show a table with 3 rows, a bulleted list, Python code and the formula x^2 + 1/2". The table has a border, code is on a dark background, the formula is readable text (x², 1/2), no raw `**` and `$`.
- [x] **E8 Token limit.** Set max tokens `30` in the profile and ask for a long story: the status shows `Max tokens limit reached, the reply was cut off`. Set the value back after the test.
- [x] **E9 Errors.**
  - A profile with a nonexistent model: a grey "System" line with the server error (or the warning `Model "X" answered instead of "Y"`).
  - Stop the server in LM Studio: the line "System: Cannot reach …".
  - No profile selected: status `Choose a model on the Model tab`, the typed text is not lost.

## F. Files

Add the roots: **F1**.

- [x] **F1** The Files tab → "+ Folder" → `test_sandbox\Alpha`, then `test_sandbox\Beta`. Trying to add `Alpha\sub` → `The folder is already inside "Alpha"`. Trying to add `test_sandbox` → `"…" is already added inside it: remove it first`.
- [x] **F2 Tree.** Expand Alpha: the `sub` folder first, then the files. `.git` is not visible. `image.png  (image: the path goes into the request)` is marked, `big.txt  (too large, 750 KB)` is grey and has no checkbox.
- [x] **F3 Checkboxes.**
  - Check `readme.md`: at the bottom `Files: 1 · ≈N tok.`, `Alpha` shows "−" (partial).
  - Check the whole `Alpha`: `Files: N · … · skipped: 1` (`big.txt` is skipped; `.git` is not counted).
  - Uncheck `sub`: the count drops, `Alpha` shows "−" again.
- [x] **F4 Files in the request.** Check `Alpha` and ask: "What is the secret word given in the project? Answer in one word." The answer: PURPLE-MAMMOTH-42. The Request window has the `Context files` block with `=== Alpha/readme.md ===`; no text from `.git\config`. Ask "What does cp1251.txt say?": the Russian text reads without garbled characters.
- [x] **F5 Size.** At the bottom the second line is `Whole request ≈X tok. of L` (L is the loaded model's context, the number from LM Studio). The number grows with the chat history.
- [ ] **F5a The real token count.** After the model answers, a line `Last request as counted by the server: N tok.` appears under the tree. It comes from LM Studio's reply and is more exact than our estimate. After several answers `Estimate corrected from the server's measurements (×0.9)` may appear (the estimate adapts to the selected model's tokenizer).
- [ ] **F5b Context overflow protection.** Load a small model with a small context (for example 4096 tokens in the LM Studio settings) and check files that clearly will not fit. Sending is refused: status `The request ≈… tok. and a reply of up to … do not fit into the model's context (4 096). Uncheck files, start a new chat or increase the context in LM Studio.` No request goes to the server, the text stays in the field, nothing is written to the history. Uncheck the files: the message is sent. If the context is unknown (the model is not loaded), the guard does not get in the way.
- [ ] **F5c Overflow in the middle of a chain.** If tool results over several rounds filled the context, the answer stops with the line "System: The model's context is full…" instead of sending too large a request. The message advises starting a new chat. The completed calls stay in the history.
- [x] **F6 Selection per chat.** In chat A check `Alpha`. "+ New chat": no files selected. Go back to A: the checkboxes and expanded folders are in place. Restart the app: the same.
- [x] **F7 "Refresh".** Create a new file in `Alpha` with Explorer, press "Refresh": the file appears, expanded folders stay expanded.
- [x] **F8 The file is read again.** Check `readme.md`, ask a question. Add the line "Password: CAT-7" to the file, ask about the password: the model sees the new version.
- [x] **F9 Removing a root.** Select a root, "− Folder": it disappears from the list, the selection is cleared, the files on disk are intact.

## G. Tools

Preparation: the roots `Alpha` and `Beta` are added (step F1; checkboxes on the Files tab are not needed). On the Tools tab check `toolbox`: all its tools are enabled.

- [x] **G1 Enabling.** In a new chat tools are off, at the bottom `No tools enabled…`. After enabling: `Enabled: N. In the request ≈… tok. (3 meta-tools and the catalog). Full schemas would take ≈… tok.`
- [x] **G2 Calculation.** "What is 1234*5678? Use the tools." The chat shows `▸ toolbox.calc(...)`, the result `◂ toolbox.calc 7006652`.
- [x] **G3 Time.** "What time is it on the computer now?" → `toolbox.time_now`.
- [ ] **G4 Folder list.** "What is in my folders?" The model should show both: Alpha and Beta (`fs_list` without path or `fs_roots`).
- [ ] **G5 Reading.** "Show the contents of the file readme.md" → `fs_read`, the answer has the real text of the file.
- [ ] **G6 Search in all folders.** "Find the string yellow sausage in my files and tell me where it is" → `Beta\requirements.txt`, line 3.
- [ ] **G7 Outside the allowed folders.** "Read the file C:\Windows\win.ini" → `◂ ERROR: Path outside the allowed folders…`, the content is not shown, the model reports the refusal.
- [ ] **G8 No file.** "Read the file missing.txt" → `◂ ERROR: file not found…`. The model must not make up the content (if it did, that is model behavior, note it).
- [ ] **G9 A note (with a question).** "Write a note demo with the text: Hello from Beta". The window `Allow the tool call?` appears with the lines `Note: demo` and `Text: Hello from Beta`.
  - "No": the answer says `The user declined the call`, there is no file.
  - Repeat, "Yes": `data\notes\demo.md` is created.
- [ ] **G10 Replacing in a file (with a question).** "Replace yellow sausage with brown bear in the file requirements.txt". The window shows `File / Was / Will be`.
  - "Yes": `Done: replaced 1 occurrence … (line 3). Backup of the original file: …`.
  - Check `Beta\requirements.txt`: it says "brown bear", the other lines are intact, the line breaks are unchanged. Next to it is `requirements.txt.bak` with the original text.
- [ ] **G11 A non-unique fragment.** "Replace apple with pear in the file dup.txt" → `◂ ERROR: the fragment old occurs 2 times…`, the file did not change. The model may widen the fragment itself (depends on the model).
- [ ] **G12 Forbidden files.** "In the file .git/config replace secret with public" and "in image.png replace PNG with X" → `ERROR: this file cannot be edited…`, the files are untouched.
- [ ] **G13 Permissions.** Right-click `fs_replace` in the tree:
  - "Run without asking": the `[asks]` tag disappears, the next replacement runs without a window (careful: this is a real write; run the script from step 0 to restore the files).
  - "Block": the `[blocked]` tag, the model gets a refusal without a window.
  - "Reset…": `[asks]` again.
- [ ] **G14 Read permissions.** Switch `calc` to "Ask for confirmation": a window appears on a call. "Reset" brings back no questions.
- [ ] **G15 No suitable tool.** "Send an email to test@example.com". The model should answer that there is no tool for this and not claim it sent it (depends on the model).
- [ ] **G16 Tools per chat.** In chat A they are on. "+ New chat": off, at the bottom `No tools enabled…`. Go back to A: on. Restart: the same. Uncheck only `fs_search`: the Request window does not have it in the catalog, and when asked to search the model answers `not in the catalog`.
- [ ] **G17 Stop in the middle of a chain.** Give a multi-step task ("find yellow sausage, read the file and count the lines") and press "Stop". Status `Stopped`, the chat has no calls without results. Restart the app and write "continue": the dialog goes on without errors.
- [ ] **G18 Server config.** The "Config" button opens `data\mcp.json`.
  - Add the line `"disabled": true` to `toolbox`, "Restart" → `○ toolbox — disabled in the config`. Remove it, "Restart" → `●` again.
  - Break the JSON (delete a bracket) → at the bottom `Could not connect the servers: …`. Fix it and restart: it works.
  - Add a server with a nonexistent command → `✗ name — error`, with the reason under it; `toolbox` keeps working.
- [ ] **G18a A path without the folder.** "Read the file requirements.txt" (it lies in a subfolder of one of the folders, not in the root). The result is `◂ ERROR: Path not found: requirements.txt …` with the hint `Did you mean: …\KeyboardEscape\requirements.txt`. This is not "outside the allowed folders". On the second try the model should use the full path.
- [ ] **G19 Closing.** Close the window during generation: it closes without hanging. `data\crash.log` has `=== exit … pid=N` for this run (the same `pid` as its `=== start`). No extra processes are left:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tools_server.py*' } | Select-Object ProcessId, CommandLine
```

## G2. Web (local search and reading pages)

Preparation: the Web tab, tools enabled. Any small documentation site works for the check. Back up `data\` first.

- [ ] **W1 Adding a site.** Enter an address (for example `docs.python.org/3/tutorial`), depth 1, pages 30, "Add and crawl". The status is `crawling…`, at the bottom `Pages saved: N` grows; at the end `Done: the index has N pages of the site.`, and the table shows the page count and the crawl time.
- [ ] **W2 Stopping.** Start crawling a large site and press "Stop": the status is `stopped`, the window does not hang, the pages collected so far remain.
- [ ] **W3 Search.** "Search my index for how list comprehensions work". The chat shows `▸ toolbox.web_search(...)`, the answer has titles and addresses of pages from the added site.
- [ ] **W4 Reading.** Ask to read one of the found pages: `toolbox.web_extract`, before the call the window `Address: …` (the tool asks). After "Yes" the answer has the page text and the list of links.
- [ ] **W5 A long page.** `web_extract` on a big page (for example `https://docs.python.org/3/whatsnew/3.15.html`): the first chunk is up to ~9000 characters, at the end `[Showing characters 0–N of M. To continue: web_extract with the same url and offset=N.]`, the list of links below. Ask "read on": the model calls `web_extract` with that `offset`, the next chunk arrives without links and without repeats. The last chunk ends with `This is the end of the page`.
- [ ] **W6 A page with JavaScript.** Ask to read a site drawn by scripts (an SPA): the answer has `(page rendered with JavaScript)` and the real text. The first run takes 2–5 seconds.
- [ ] **W7 Local addresses are blocked.** "Read http://localhost:1234/v1/models" and "http://192.168.0.1" → `ERROR: … non-public address`. `file:///C:/Windows/win.ini` → an error about http/https.
- [ ] **W8 An empty index.** Delete all sites ("Delete", confirmation) and ask the same as in W3: `ERROR: the index is empty…`; the model does not make up results.
- [ ] **W9 Data, not instructions.** The first line of a `web_search` and `web_extract` result is the notice "External data from the internet…".
- [ ] **S1 SearXNG: check.** On the Web tab enter the address of your SearXNG (for example `http://192.168.0.50:8080`), "Check": `Working: the test query returned N results.` If you get `403`, enable `search.formats: [html, json]` in SearXNG's `settings.yml` and restart it.
- [ ] **S2 SearXNG: search from the chat.** "Search the internet for what is new in Python 3.15" → `▸ toolbox.web_search_internet(...)`, without a confirmation window; the answer has titles, addresses and snippets.
- [ ] **S3 Search + reading.** After S2 ask to read the first result: `web_extract` asks for confirmation with the address, after "Yes" the page text arrives.
- [ ] **S4 SearXNG off.** Clear the field and ask the same: `ERROR: The SearXNG address is not set…`, the model honestly says there is no internet search. The address is read again on every call, no restart needed.
- [ ] **W10 Closing during a crawl.** Close the window while crawling: it closes without hanging (up to 8 seconds), no extra `python`/`QtWebEngineProcess` processes remain.

## H. Data

- [ ] **H1** After a restart these are kept: chats and history, connections, profiles, the roots on the Files tab, the selection of files and tools per chat, tool permissions.
- [ ] **H2** Deleting a chat erases its history and its selection of files and tools (create a new chat: nothing came along).
- [ ] **H3** Entries in `data\crash.log` are tied to the process id: `=== start … pid=N` and `=== exit … pid=N`. Old entries without `pid` (before 15.10) are not paired. If a run broke off, the next start adds `=== lost … pid=N (the run started … did not end normally: the process is gone)`: that is an abnormal termination, send me the lines around it. The app running right now has no `exit`, which is normal, as are parallel instances. Before every request to the model the same file gets a line `[req] stream … request≈N tok. (raw estimate …) limit=L rss=… ram_free=…`: if the app closes silently, the last such line shows how large the request was and how much memory was left.

---

## What not to count as a bug

- Small models sometimes call a tool with the wrong path, forget an argument or answer without a call. Look at the `▸`/`◂` lines in the chat: if they are right but the model's retelling is not, that is model behavior.
- Response time depends on the hardware, not on the harness.
- The token estimate is approximate: on text it is off by 5% on average, on code it may underestimate by up to 20%. So the overflow guard keeps a margin (+15%) and refuses a little earlier than the context actually fills up. The server sends the exact number after the answer.
- Scanned PDFs without a text layer are not read. UTF-16 is read but not edited.
- `.bak` copies pile up next to edited files.
- Tool results stay in the chat history and take up context (the "Whole request" line shows this).
- Limits: one tool result up to 12,000 characters, a call up to 60 seconds, up to 16 tool rounds per answer (configurable in the profile). When the limit is reached, the model is asked to sum up without tools, and the chat shows the warning "Reached the tool step limit". If you need a multi-step task (for example editing several files), give it one file at a time.
- A non-unique fragment in `fs_replace`: the model should widen the fragment itself. Not every model manages, this is not a harness bug.
- Roots on the Files tab are added and removed as a whole. A root's subfolder cannot be removed separately, but you can uncheck any subfolder in the tree.

## If something is wrong

Send:

1. The step number and what was expected versus what happened.
2. A screenshot or the chat text (it now keeps "System" lines with errors).
3. The content of the Request window for this chat.
4. The last lines of `data\crash.log` and, for tool problems, `data\logs\mcp-toolbox.log`.

After testing you can restore the data from the backup (`data_backup`) and delete the `test_sandbox` folder.
