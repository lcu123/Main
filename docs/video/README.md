# Zest Visit Video

A tech records one short video per property visit (one per tech: when two techs
each have work at a property the same day, each sends his own). The customer gets an email
with a link to watch it. Built from the Visit Video build brief (Nov 12, 2026).

| Piece | Where | What it does |
| --- | --- | --- |
| iPhone app | `ios/` | Pick the stop, record 15 seconds, one tap to save. Uploads by itself. |
| Video server | `src/zest_video/` | The only thing the app talks to. Holds the FieldRoutes keys, tracks every video, sends the email, serves the watch page and the office page. |
| App ↔ server contract | [`API.md`](API.md) | Every call the app makes. |
| Owner's checklist | [`GO-LIVE.md`](GO-LIVE.md) | What is left before techs can use it. |
| Apple submission | [`APP-REVIEW.md`](APP-REVIEW.md) | The usual rejection reasons, where we stand, and what to type in App Store Connect. |
| Tech's one-page guide | [`TECH-GUIDE.md`](TECH-GUIDE.md) | Hand this to the techs. |
| Pictures of the app | [`screens/`](screens/00-contact-sheet.png) | Every screen, photographed from the real app by CI at App Store size. |

The server ships in the same Docker image as the FieldRoutes MCP server and
runs as a second Railway service (`zest-video`, start command `zest-video`).
It reuses `fr_mcp.client.FieldRoutesClient`, so auth, wire format, rate limit
and the shared daily API quota are handled once.

## How a video moves

```
phone:   record → SAVE → on disk → register → upload to bucket → confirm → delete local file
server:  awaiting_upload → held (tech's 10-minute undo) → sent → watched
                                                        ↘ no_email (link live, no email of ours: reason kept)
         unmatched (filmed as "not on my list", office picks the customer)
         pulled / deleted / replaced (link dead)
```

- The video ID is made on the phone. Register, confirm and delete are all safe
  to repeat, so a retry on bad signal never makes a second video or a second email.
- Video bytes never pass through the server. The phone uploads straight to a
  private bucket with a signed URL; the watch page plays through
  `/w/<token>/video`, which redirects to a fresh short-lived signed URL.
- The customer link is a random 128-bit token. No login, marked noindex, rate limited.
- Recording again inside the 10 minutes replaces that tech's old video by itself. After
  the email has gone out the stop is locked for him; the office can pull or move it.
- The office can leave a note on any row of the day page (a stop with no video, or a
  video). Notes are kept in the audit trail with who and when; the row shows the newest.
- One video per tech, per property, per day. Two techs with appointments at the same
  property each send their own video, tied to their own appointments; the customer gets
  one email per video, naming the tech and the service. A missing video is flagged for
  the tech who owes it. A helper's video covers the stop for the lead.
- The owner can have FieldRoutes' own after-service email carry the link instead of
  ours: one email, not two. It is switched off. See "One email" below.

## One email: the link inside FieldRoutes' own email

The owner's decision of Nov 14, 2026: after a visit the customer gets one email, not
two, with the video one tap away inside it. So the link rides in the email FieldRoutes
already sends. Built, tested on the fake FieldRoutes, and **switched off**: the setting
`fr_email_minutes` on the office's Settings page is 0, and at 0 the server does exactly
what it did before. The write it rests on has **never been run on the live account**;
[`GO-LIVE.md`](GO-LIVE.md) has the test the owner runs on the test customer first.

How it works:

1. In FieldRoutes the office sets the after-service email ("Service Followup Email") to
   go 30 minutes or 1 hour after a visit is completed, and puts `{{techComments}}` in
   its body. FieldRoutes prints the technician's comments there. Nothing in the API
   reaches either setting.
2. On our Settings page the owner picks the same number (30 or 60). The page refuses a
   tech's hold that does not fit inside it (the hold has to be 10 minutes shorter), and
   refuses it while "Overdue hours" is 0, because that number is the only limit on how
   long a video waits for its visit to be completed.
3. When a video is let go (the tech's hold is over, and every check that lets a video
   go out today has passed: the route, the overdue rule, a customer who stopped video
   emails, no email address on file), the server looks at the tech's own appointments
   at that stop:
   - **None completed yet**: nothing is due. The video is still `held` meanwhile, so
     the tech can still delete it or record again. During work hours the day's list
     is read every five minutes anyway and the video works from that. After hours,
     and for a day that is over, the list is read for it every 15 minutes (with 30
     picked) or every 30 (with 60): half of FieldRoutes' delay, so a completion is
     seen with the other half left to add the line in (`videos.wait_read`). No
     customer and no bucket is read for a waiting video. The existing overdue rule is
     the only limit on the wait.
   - **One is completed and its email has not gone**: the first such one is picked. "Not
     gone" means now is at least 5 minutes before completion time + the 30 or 60. In
     one save the video is settled (`no_email` with `email_skip = fr_email`: it went
     out, and the tech's stop locks as for any sent video), the usual internal note is
     queued, and one outbox row of the kind `comment` is queued with the appointment,
     the line, and its deadline.
   - **Completed, and too late for every one**: before it says so the server reads
     those appointments once. If the link is already in the comments (a database
     backup was put back, and the line went in before it), the video is settled as in
     FieldRoutes' email. If not, it is held for the office (`email_skip = fr_missed`,
     flag "Video not in FieldRoutes' email: the office decides"), like the other
     holds: Send it anyway (our own email) or Pull. No second email goes by itself.
4. The outbox works on the line, comment rows first. A row is looked at more than
   once (`videos._write_comment`):
   - **Writing.** `fr.add_to_tech_comments` reads the appointment fresh, refuses for
     good unless it is that customer's, its status is exactly "1" and the read carries
     the technician's comments, keeps his own words, and adds a blank line and our
     line: `appointment/complete` with exactly `appointmentID` and `completionNotes`.
     The write follows the read at once and is sent one time: the shared client's own
     retry is switched off for it, and the line does not start unless there is room
     under the per-minute limit for all its calls, because a retry or a wait for room
     would put up to minutes between the read and the send. Then the appointment is
     read again. The line counts as in only when that read shows it.
     If anything else on the visit reads differently (status, completion time, who
     serviced or completed it, the times, the invoice number) or his words did not
     come back as they were, the office gets the flag "A visit changed in FieldRoutes
     when a video link was added: check it". If the link is already in the comments
     nothing is written, so a try cut off by a restart, or one whose answer was lost,
     is simply tried again. When FieldRoutes does not answer, the next try is a minute
     later, then two, two, three and every five, the last one a minute before the
     deadline.
   - **Too late.** Past the deadline nothing is written to that appointment. The
     server reads it first: an earlier try may have got through. Link there: it is
     in. Not there: the tech's next completed appointment at the stop whose email has
     not gone gets the line, or, when there is none, the row is closed and the video
     becomes the same hold in one save. Could not read: the flag says the server
     cannot tell, and to look in FieldRoutes before sending anything; the server
     reads again every five minutes for up to six hours and then puts the flag
     right (`videos._settle_unsure`), or closes it if the link is there after all.
   - **The last look.** Two minutes before the deadline the appointment is read once
     more. The comments can be saved again after the line went in (the tech fixes his
     note in FieldRoutes' app, a phone that was offline hands in its copy late), and
     that save has no line of ours. Still there: done. Gone: the line is added again
     under the new words. Gone and too late: the hold, with a flag that says what
     happened.

What decides whether a video goes this way: the setting is 30 or 60, `FR_WRITES` is on,
and the customer is inside `FR_WRITE_CUSTOMER_IDS` when that list is set. That list is
how the owner tries it on the test customer alone; everybody else's video then goes as
before. Once a person presses Send it anyway on a video, it goes as our own email.

Why the write is fenced in: `appointment/complete` is the call the mobile app uses to
complete a visit. Its `status` defaults to Completed and it runs FieldRoutes' invoicing
logic, so on a pending appointment it would complete it and make a real invoice.
FieldRoutes documents that on an already-completed appointment it only updates what is
sent. The server never sends a status, reads immediately before every write, and
`tests/test_video_fr_email.py` proves on the fake that it never calls it on anything
but that customer's completed appointment. What stays open is the fraction of a second
between FieldRoutes answering the read and taking the write: a visit set back to
pending in exactly that moment would be completed by the call. Nothing outside
FieldRoutes can close that. The read after the write sees it and the office is told.

What the office sees: "Waiting for the tech to complete the visit" until he does,
"Going into FieldRoutes' email" while the line waits to be written, "Link added for
FieldRoutes' email · 2:36 PM" once it is in, and "In FieldRoutes' email · 2:36 PM"
after the last look (only then is it counted as sent in the stats). The server cannot
see what happens in the last minutes before FieldRoutes sends, or whether it sent.
The line stays in FieldRoutes whatever happens to the video later: after a pull, a
"not my home" or the keep time, the link in an old email or report opens the page
that says the video is no longer available. A video that is pulled and put back on
the same customer gets no second line; it waits for the office.

A deploy rolled back to a server from before this was built: that server marks an
outbox row of a kind it does not know as done and writes nothing. The new server does
not take such a row for a line that went in (only its own marks count,
`videos._line_in`) and puts it back in line. The old server also does not know the
setting, so set it back to "Our own email" before rolling back.

## What the server checks by itself

| Check | When | Result |
| --- | --- | --- |
| Stop finished, no video | 60 min after FieldRoutes shows it done, and only for techs signed in on a phone | Office flag. Clears itself if the video arrives late. |
| Video stuck on a phone | 30 min, then 3 hours | Push nudge to the tech, then an office flag. |
| Recorded outside the check-in time | When the email is sent | Office flag. Never blocks. |
| Tech sent past a two-keys warning | At register | Office flag and an audit entry. Never blocks. |
| Customer has no usable email | When the email would go | Link saved as a FieldRoutes note, office flag. |
| The visit left the route, or it is a second video for one visit | When the email would go | The video waits for the office: no email, nothing written, an office flag, "Send it anyway" and "Pull". See "When the route changes during the day". |
| The email is hours late | When the email would go, and when the route would let a waiting video go | The same: it waits for the office. Its flag also says what the route says about the visit. See "What survives a restart". |
| The video is too late for FieldRoutes' own email (only with "One email" switched on) | When the video is let go, and again just before its line is written | The server reads the visit first. Link already there: it is in. If not: it waits for the office, with the flag "Video not in FieldRoutes' email: the office decides". No second email goes by itself. |
| The line is still in the technician's comments (only with "One email") | Right after it is written, and once more two minutes before the last moment it can be added | Gone at the last look: added again. Gone and too late: it waits for the office, with a flag. |
| Nothing but the comments changed on the visit when the line was added (only with "One email") | Right after every write | Office flag at once: "A visit changed in FieldRoutes when a video link was added: check it". |
| The file is in the bucket and is the one that was confirmed | Before an email goes and each time a customer presses play | A file with another ETag, or one the bucket itself says is gone, is pulled and flagged. On an unclear answer (a bare "not found", a page of words, no ETag) nothing is pulled. |
| FieldRoutes write failed | After 8 tries over a day | Office flag. Writes wait in an outbox meanwhile. |
| FieldRoutes write cut off by a restart | When the server is back | Not tried again, so nothing is written twice. Office flag, with what to add by hand. |
| Tech no longer active in FieldRoutes | Daily, and at login | App locks. |

## When the route changes during the day

The office cancels, adds and moves stops all day, and a phone with no signal
works from the list it fetched that morning. The rules, each one pinned by
`tests/test_video_route_day.py`:

- **A video is never refused or lost because the route changed.** The phone
  hands in what it filmed, for the stop it filmed it at.
- **A visit taken off the day** (cancelled, deleted, a no-show, moved to another
  day) leaves the phone at its next fetch and nobody is chased for it. A video
  filmed for it and not yet emailed is kept and held back: no email, nothing
  written to FieldRoutes, an office flag, and two buttons on its row, "Send it
  anyway" and "Pull" ("Keep it" while customer emails are off). If the visit
  comes back the video goes out by itself. Two things stop that: a person has
  cleared its flag (then it waits for Send or Pull and a flag says so), or its
  email is hours late by then (then it waits as any late video does; see
  "What survives a restart"). If the email had already gone, the office gets a
  flag saying that. Owner's switch: Settings, "hold the email until the office
  says send".
- **No email goes on a list nobody could check.** If the day cannot be read
  from FieldRoutes when an email is due, the email waits a few minutes, as it
  does when the customer's address cannot be read.
- **A stop given to another tech** moves from one phone to the other. A video
  the first tech already filmed still goes out, with a flag naming who has the
  stop now. It counts as the second tech's video (his phone shows the stop as
  filmed, he is not chased) only once it is uploaded, while it is not pulled,
  and when all his work at that stop is the work that was filmed. With other
  work of his own there he owes his own, like any two techs at one property.
- **One visit, one email.** If both techs film a stop that changed hands, the
  video of the tech who has it now goes and the other waits for the office; if
  the first had already gone, the later one waits ("Two videos for one visit").
- **Each change is told once.** One route flag is open per video at most, the
  one that is true now. When the route changes again the old flag is closed and
  the new thing gets its own, even if a person cleared the last one.
- **"Finished with no video" follows the stop.** The flag clears for a tech who
  no longer has the stop and comes back if the stop does, or if the video that
  cleared it is deleted (or, after a handover, pulled). A flag a person cleared
  stays cleared.
- **A filmed visit the office rolls to another day** is on that day's list
  again and the app asks for a video again, as before. The office gets a flag
  that it was already filmed, and "finished with no video" says so too. Whether
  the first video should count for the new day is one of the owner's choices.
- **A video for a past day** (it sat on a phone overnight) is checked against
  that day's route when its email goes. Yesterday's are checked once more after
  the 3 a.m. read, which also flags work closed out after hours with no video.
- **The tech is told when the office is holding his video.** Every video in
  the phone's day carries `withOffice` while it waits for Send it anyway or
  Pull, and the app shows SENT TO OFFICE where it would show SENT. He cannot
  delete it or record over it meanwhile; the phone tells him to call the office.
- **Order does not matter.** A tech may work the stops in any order, film before
  he checks in or after he checks out, or close out every appointment in one go.
  Every video goes to the customer it was filmed for. Only one filmed more than
  20 minutes outside the check-in time raises a flag, and it never blocks.

How soon a change is seen: the server reads the day from FieldRoutes every 5
minutes in work hours once a tech has the app, and whenever a phone or the
office page asks and the saved list is over 3 minutes old. While FieldRoutes
answers that is at most 20 reads an hour, 40 on a day when an appointment names
no tech: under a fifth of the 3,000 shared reads over a 14-hour day. While it
fails it is tried once a minute and no more. Checking videos and flags against
the route costs none. When FieldRoutes is slow or down, phones are served the
saved list at once, straight after a restart too.

Each rule above that is a judgement call is listed for the owner, with what the
other choice would mean, in [`GO-LIVE.md`](GO-LIVE.md) under "Choices waiting
for you".

What it cannot do: if the office cancels a visit in the last 3 minutes before an
email is due, the email goes on the list read just before, and the office is
flagged on the next round. A phone that names no appointments for a stop that is
no longer its tech's cannot be matched to the work, so that video waits for the
office.

## Rolling it out to one tech first

Nothing has to be switched for a pilot. A tech counts only once he has signed
in on a phone: until then his stops are not flagged, not counted as missed, and
show "Tech has no app yet" on the office page.

## Safety switches

Nothing reaches a customer or FieldRoutes until someone turns it on.

| Variable | Values | Default |
| --- | --- | --- |
| `VIDEO_EMAIL_MODE` | `off`: no customer email. `test`: every customer email goes to `VIDEO_EMAIL_TEST_TO`. `live`. | `off` |
| `VIDEO_EMAIL_CUSTOMER_IDS` | Only these customers are ever emailed. | unset |
| `FR_WRITES` | `off`: no notes, reviews or tasks are written. | `on` |
| `FR_WRITE_CUSTOMER_IDS` | Only these customers are written to. | unset |

`FR_WRITES` and `FR_WRITE_CUSTOMER_IDS` mean the same thing here as on the MCP
server. Every other variable is listed in [`.env.example`](../../.env.example).
Things the owner can change without a deploy (video length, hold time, alert
timing, email wording, which services need a video, whether a video for a visit
that left the route waits for the office, how many hours late an email may
be before it waits too, and which email carries the link) live on the office
page under Settings.

"One email" (`fr_email_minutes`, off) is a setting and not a variable, so the
owner can switch it off in a second. It does nothing while `FR_WRITES` is off,
and with `FR_WRITE_CUSTOMER_IDS` set it reaches only those customers.

## What survives a restart

Railway replaces the server's container at every deploy and may restart it at
any time. The container's own disk and memory are wiped each time, so nothing
that matters is kept there. Records are in Postgres, a separate service with its
own volume. Videos are in the bucket: the phone uploads each file straight to it
with a signed link, and the server never holds a file.

A start goes through three gates before the server takes a request or runs a job:

1. **The settings.** A real deployment does not start without
   `VIDEO_DATABASE_URL` (Postgres, not a file) and all four bucket settings
   (`VIDEO_S3_ENDPOINT`, `_BUCKET`, `_ACCESS_KEY_ID`, `_SECRET_ACCESS_KEY`). It
   prints which one is missing and exits. On Railway a deploy that does not start
   never takes over, so the version before it keeps serving. "Real" means either
   Railway's own variables are there or `VIDEO_BASE_URL` is a public https
   address; a laptop, a test and CI have neither and start with a warning.
   `VIDEO_ALLOW_THROWAWAY_STORAGE=1` lets a demo through. (So a test rig given
   a public https address, such as `tests/browser/serve.py --base-url
   https://...` for a real TV, needs that variable set too.)
2. **The database.** It is asked until it answers, for up to five minutes, so
   Postgres restarting beside the server does not leave the server down. A stop
   signal ends the wait at once, in a container too. An address the driver will
   never take (`?sslmode=...` on the end, another driver's name, a port that is
   not a number) is refused at once and not waited for.
3. **The bucket and the database belong together.** The newest videos the
   database knows are looked for. If the storage says the key, the bucket name
   or the region is wrong, or says in its own words that it holds none of them,
   the server does not start: on the wrong bucket it would pull every video a
   customer opened. (`VIDEO_START_WITH_FILES_MISSING=1` starts it anyway, for
   when the files really are gone.) And the other way round: a database that
   knows of no video at all, and that no phone has ever signed in on, beside a
   bucket that holds videos, is a new or an emptied database, and the server
   does not start on it either (`VIDEO_START_WITH_NEW_DATABASE=1`, for a
   database that really is new). If phones have signed in on it, it is the
   database in use, with old files beside it (test files, or videos from after
   a backup that was put back): the server starts and says so in a warning. A
   bucket that cannot be reached, or answers with somebody else's page (a "not
   found", or a page of words with a 200 on it), settles nothing and does not
   stop a start.

A refused start says why once, in plain words, with no stack of code and no
password in it. An override that is set is named in the first line of every
start, with a warning to remove it, whether or not that start needed it.
`FR_WRITES` is read by the connector's own rule (on only for `on`, `1`, `true`,
`yes`); a word the server does not know is read as off and it says so.

`/healthz` says `ok` only while the database answers. It never asks the bucket,
FieldRoutes or the mail service: each has a fallback that loses nothing (the
phone keeps its video, writes wait in the outbox, the saved route is served), so
a bad minute at one of them must not make a working server look dead.

What lives only in memory, and what a restart costs:

| In memory | What a restart costs |
| --- | --- |
| Rate limits per address | They start again from zero. The lock after 5 wrong tries on a login code is in the database. |
| When FieldRoutes was last read, and the service names | A handful of reads at the next start, eight at most of the 3,000 a day: the day's stops (and yesterday's once more), the tech list, the service names, and the routes on a day with an unassigned appointment. No customer is read again. |
| Job timers | Every job runs once at start, the daily clean-up included. Each works from times saved in the database, so nothing is early, late or done twice. |
| The settings cache and the FieldRoutes quota count | Read again from the database, and from FieldRoutes' next answer. |
| Requests in flight | The phone and the browser ask again. Every phone call is safe to repeat. |

Everything else is in the database: sign-ins and login codes, the day's stops
and when each was first seen finished, every video and when its email is due,
the outbox and its retry times, flags, scores, opt-outs, settings and the audit
trail. There is no list of upload links: a link is signed with the bucket's key
and checked by the bucket. `tests/test_video_restart.py` restarts the server at
every step of a video's life (after register, after the upload, all through the
10 minutes, with an email half sent, with FieldRoutes writes waiting, with a
"no video" check due) and checks that one email goes out, at the right time.
It also kills the server before and after every save in a video's life, runs
two servers side by side over one database, leaves the server down for hours,
and puts a backup back. `tests/test_video_durable.py` covers the gates, the
health check, and what the server makes of each answer a bucket can give.

Killed at any moment, and nothing is lost or done twice. Five gaps were found by
these tests and are closed; the tests that pinned them are plain tests now.

- **A record and what follows from it are one save.** "Sent" is written
  together with its audit entry, the FieldRoutes note in the outbox, and the
  follow-up task and flag when the tech recommended something. "No email" is
  written with its flag and its note. A confirmed video not on the list is
  written with its flag; a new video with its audit entry and its "sent past a
  warning" flag; a score with its review and its low-score flag; a pull, a
  move, an opt-out and "Send it anyway" each with their audit entry. A server
  killed at that moment leaves all of it or none of it, and with none the step
  runs again (the mail service takes the same email only once). The few steps
  that cannot be part of the save, because they look outside the database, are
  safe to repeat and are repeated: when the phone asks again, or on the next
  five-minute round. One test kills the server before and after every save in a
  video's life and checks the result each time.
- **Two servers at a deploy.** Railway starts the new server before it stops
  the old one, so for a moment both run the job loops. Whatever both reach for
  is claimed first, with an update only one of them can win: an email, a push,
  and now each FieldRoutes write. Flags and outbox rows have a key that can
  exist once. A claim runs from the moment it is made, not from when the pass
  began: a pass slowed by FieldRoutes timing out can be many minutes old when
  it reaches a row. Tests run two servers over one database, side by side, in
  the very same instant and in the middle of a slow pass, and each email,
  push, flag and write happens once.
- **A FieldRoutes write cut off in the middle.** A row is marked as being
  written when it is claimed. If the server is stopped before the answer comes,
  nobody can say whether FieldRoutes took the write, so it is not tried again:
  the office gets a flag that says to look, with the words to add by hand.
  "Stopped" is read off the row itself: marked as being written, and its claim
  run out. A row another server is writing right now is not that.
- **An email that is hours late does not go by itself.** After the server or
  FieldRoutes was down for hours, the late emails used to go out the moment
  things worked again, whatever the hour. After a database restore the same
  rule sent second emails, and sent videos the office had pulled. Now a video
  whose email is more than 6 hours late (Settings, "Overdue hours"; 0 switches
  it off) waits for the office like a video whose visit left the route: kept,
  playable, nobody emailed, nothing written to FieldRoutes, one flag, "Send it
  anyway" or "Pull". How late is counted from the end of the tech's 10
  minutes, or from the last time a person at the office put the video back in
  line (Send it anyway, a move). The route letting a video go does not start
  the count again, and the route does not let a video go at all once its email
  is this late: so a video that was waiting for the route when a backup was
  taken is not sent a second time after that backup is put back, and nobody is
  emailed at 3 a.m. because a visit was un-cancelled in the evening.
  With customer emails and FieldRoutes writes both off nothing can go out late,
  so such a video is simply closed as "customer emails are off", which is the
  state [`GO-LIVE.md`](GO-LIVE.md) asks for before a restore.
- **What the flag of a late video says.** After a restore the server no longer
  knows that a video was emailed days ago, so the flag never says that nobody
  was emailed. It says the server has no record of an email, that the customer
  may already have it if a backup was put back, and where to look first: the
  video note on the customer's account in FieldRoutes, which a restore does
  not touch. The same words stand beside the button. The flag also says what
  the route says: that the visit has left the day, or that another tech's
  video for it went out. When that changes, the flag is replaced by a new one.
- **Send it anyway on a late video answers the lateness, nothing else.** If
  the flag in front of the office said only that the email was late, the
  route is still checked before the email goes. A visit cancelled meanwhile,
  or a second video for the visit, holds it once more, under the flag for that,
  and the office decides again. If the flag had already said so, one press is
  enough.
- **A video is pulled only on the bucket's own word.** Before an email goes and
  before a customer's player is handed a link, the server checks that the
  bucket still holds the file it confirmed. A plain "not found" used to be
  enough to pull the video, and so was any answer whose size or ETag differed;
  whatever stands in front of a bucket can say either on a bad minute (a "not
  found", a page that says "we will be right back" with a 200 on it, the right
  file with its ETag dropped or marked weak). Now the server pulls only when
  the bucket says, in its own words, that the file is gone, or answers with a
  file that has another ETag. On anything else nothing is pulled: the player
  is let through, and an email waits and is tried again every 5 minutes, with
  a flag for the office if that lasts half an hour. A bucket that cannot be
  reached at all proves nothing either way and holds nothing up.
- **An email the mail service already has.** If the server is killed after
  handing an email over and the wording changes before the second try, the mail
  service answers "that key is used" (409, `invalid_idempotent_request`). That
  proves the key was seen before, not that an email reached anyone. So it is
  read as sent only when no earlier try for that video was written down as
  failed (a kill leaves no such entry). If one was, nobody here can say: the
  server stops trying, the record says no email is known to have gone, and the
  office is asked to check. The key names the inbox, so an email that went to
  the test inbox never stands for the customer's.

Still not closed, and small:

- A restore within 6 hours of its backup is inside the limit above, so the
  safe steps in `GO-LIVE.md` (customer email and FieldRoutes writes off first)
  still matter. An opt-out made after the backup is forgotten by any restore.
- After a restore the server cannot know which late videos were already
  emailed. It tells the office to look for the video note in FieldRoutes
  before pressing Send it anyway; it does not look itself. If the office
  presses without looking, that customer gets the email a second time.
- The FieldRoutes connector itself tries a write again when FieldRoutes does
  not answer in time. If FieldRoutes took the first try and only its answer was
  lost, that note is written twice. This is the connector's own behaviour and
  was not changed here.
- A FieldRoutes write that was waiting in the outbox at the very moment a
  backup was taken is waiting again after that backup is put back, and is made
  again. The safe steps in `GO-LIVE.md` (FieldRoutes writes off first) cover it.
- The live bucket words a missing file the standard way (`NoSuchKey`): seen
  on Nov 14, 2026, when the live server's start-up check asked it about a file
  that is not there and logged "the bucket answers". Whether it answers the
  list call (asked only of a database with no video at all) has not been seen.

## FieldRoutes use

Reads, sized for the shared 3,000-a-day quota:

- The day's stops: one `appointment/search` per refresh (every 5 minutes in
  work hours once any tech has the app, and when an app or the office page asks
  with a cache older than 3 minutes), plus one `customer/get` only for
  customers not seen yet that day.
- `route/search` only on a day where an appointment has no `assignedTech`.
- `employee/search` once a day and when an unknown contact asks for a code.
- One `customer/get` per email sent, to read the address fresh. The server
  never stores customer emails or phone numbers.

Writes, all through an outbox that retries: `note/create` (the link),
`review/create` (the score), `task/create` (tech recommended something). A row
is claimed before it is written, so two servers never write the same one.

With "One email" switched on, each video costs three `appointment/get` and one
`appointment/complete` more (read, write, read back, and the last look), and
one `customer/get` as an email would. A video that is too late costs one
`appointment/get`. While a video waits for its visit to be completed, no
customer is read. During work hours it reads nothing of its own. After hours,
and for a day that is over, that day's list is read 4 times an hour with 30
minutes picked and 2 times with 60 (`appointment/search`, plus `route/search`
on a day where an appointment has no tech of its own). With "Overdue hours"
raised to 24 that is at most about 190 reads for one day's waiting videos.

Checked against a real day (Nov 12, 2026, 44 completed appointments, 4 techs):
`assignedTech` on the appointment matched the route's tech and `servicedBy` on
every one, so stops are grouped by `assignedTech` with the route as fallback.
`employeeID` on an appointment is who booked it, not the tech.

## Run it

```bash
pip install -e ".[dev,video]"
```

```bash
pytest
```

```bash
VIDEO_DATABASE_URL=sqlite+aiosqlite:///./zest_video.db VIDEO_MAIL_BACKEND=log zest-video
```

With no bucket configured the server uses an in-memory stand-in and says so at
startup. `VIDEO_MAIL_BACKEND=log` prints emails, login codes included, to the
log instead of sending them.

The iPhone app: see [`ios/README.md`](../../ios/README.md). CI builds it on
macOS for every push that touches `ios/`.

## Where the build differs from the brief, on purpose

Each of these was the owner's later call or a choice made while building. An
independent read of the brief against the code (Nov 13) listed them; they are
kept here so nobody "fixes" one by accident.

| Brief | Built | Why |
| --- | --- | --- |
| Keep, Retake, Send | One question, "Save this video?", then it uploads by itself | Owner: one prompt only |
| A second video asks "replace?" | Recording again replaces the tech's own video with no question; delete for 10 minutes only | Owner |
| Prompts: name or street, did, saw, recommend | "Hi {name}", "Here's what I did", "Here's what I recommend", "I closed the backyard gate" | Owner: say the name only |
| Code by text and email | Text first (Quo), email as a fallback | Owner: techs do not read email |
| Zest colours in the tech app | The look of the field app the techs already use; customer pages and emails stay Zest | Owner |
| Location: While Using, then Always | While Using only; the leave-a-stop reminder is built but off | App Review questions "Always" |
| The email goes a set time after upload | The 10 minutes run from the recording; a late upload goes about a minute after it lands | So the tech's undo time is the same whatever the signal |
| "Not my home": one tap | Two taps (open, then confirm) | One stray tap would kill a customer's own video |
| No stop button | A tap on the ring ends the clip early, but not in the first 3 seconds | A tech who is done at 9 seconds should not film his boots for 6 more |
| Two keys must agree | On a stop with no map pin and no check-in, the spoken name alone is enough | Nothing else can be read there; warning on every such stop would teach techs to ignore warnings |
| Search by name or street | None; with no location the whole route is listed in order | "No typing after setup" |
| Add a tech | Techs come only from FieldRoutes; the office page has "Refresh the list" | FieldRoutes is the source of truth |
| Short-lived upload link | The link lasts a day so a slow upload can finish; the server checks the file is still the confirmed one before any email and before every play, and pulls it if not | A weak-signal upload must not die at the 15-minute mark |
| Sales task category 11 | The office's own Task Category ID | The live API refuses the brief's IDs |

## What is proven and what is not

Proven:

- Server: the test suite (phone API, watch page, office page) passes on SQLite
  and on Postgres. The deployed server passed a live run with the demo account:
  sign in, register, upload a real file to the real bucket by signed URL,
  confirm. The bucket refused an upload with the wrong content type.
- URL signing matches Amazon's published SigV4 example.
- App: compiles and passes its unit tests on Xcode 16 and Xcode 26, and CI
  launches every screen in the iPhone simulator and photographs it.
- App, end to end in the simulator: 99 UI tests tap through every screen and
  button against a fake server and check both the screen and what the server
  received. They cover sign-in by text code, the route list, record → "Save
  this video?" → SAVE, record again, delete inside the 10 minutes, the
  two-keys warning, double taps, and the failures: the app killed in the
  middle of a recording (the clip is recovered and offered on relaunch),
  killed on the review screen, killed in the middle of an upload, killed
  before the server confirmed; server errors, no connection, an expired
  upload link, being signed out with a video queued. In every case exactly
  one video reaches the server and none is lost. A real background-session
  upload also arrives in a signed simulator build. The logic
  package (stop ranking, spoken-name matching, two-keys rule, queue state
  machine, 10-minute window, phone-number formatting) has its own test suite
  that also runs on Linux.

- The customer's pages, in real browsers (`tests/browser`, the workflow "Customer
  pages in real browsers"; start with `tests/browser/README.md`). The watch page,
  the dead-link page, unsubscribe, privacy, terms, support and the email are
  opened in Chrome's, Firefox's and Safari's engines at 38 screen sizes: iPhones
  from the SE to the 17 Pro Max, upright and on their side; four iPads; Android
  phones; laptops and monitors up to 4K; three TV browsers. Real Safari on a Mac
  and real Mobile Safari in the iOS Simulator open the same pages. On each one
  the video plays through the real redirect to a bucket on another address, a
  score is sent, and the page fits with no sideways scroll. Also run: script
  switched off; a page left open past the life of its video link; a video taken
  down while the page is open; a weak signal and a lost one; a file with its
  index at the end; dark mode, a page the browser has darkened, and Windows
  contrast themes; 200% and 400% zoom; a keyboard alone; a TV remote (arrows,
  OK, and the play and pause keys). The run of November 14: on Linux every
  check passed across all 38 sizes and the three engines (over 1,100 checks);
  on a Mac, Safari's engine with Apple's own video stack passed all 596 checks
  on the iPhone, iPad and desktop Safari sizes; and real Safari passed on the
  Mac, on an iPhone SE, an iPhone 17 and an iPhone 17 Pro Max, and on an iPad,
  in Apple's simulator (the video played, and a page whose link had expired
  still played to the end). The deployed page was also opened read-only at the
  14 Chrome-family sizes, playing a real file from the real bucket: 28 of 28
  passed. What the suite still excuses is listed in
  `tests/browser/lib/known-gaps.js`, each entry with what the customer sees;
  there are five, and none stops a customer watching
  (the largest: with script switched off, Safari draws no player buttons, so
  the page offers a link that opens the video by itself).

  Running the page in Firefox found one fault no other browser showed: now and
  then a frozen video got no "your signal is weak" note, because Firefox sends
  the player's events in an order the page did not expect. The page no longer
  leans on those events: while a video is playing it looks at the picture once
  a second, and says so when it has not moved for six.

  What a customer gets on each kind of screen:

  | Screen | What happens |
  | --- | --- |
  | iPhone | The page fits; the video plays in the page with the phone's own player and its full-screen button. On its side the player fills the screen. |
  | iPad | The same page as a centred column with a bigger player. It plays in the page. |
  | Android phone | The same, in Chrome, Samsung Internet and the browser inside a mail app. |
  | Laptop or desktop | A centred column; mouse or keyboard. On a very tall or 4K screen the player grows. |
  | TV browser | The page opens and the player takes about three quarters of the screen height. A pointer remote presses play; with arrow keys, OK plays and pauses, and the remote's play and pause keys work. Full screen works. The words around the video are small from a couch; that was left as it is, since the video is what a TV is for. |
  | AirPlay or Chromecast | Nothing on the page blocks either, so a customer can send the video from a phone to a TV. |

- Server under stress (`scripts/video_load_test.py`, on Postgres): 200 techs
  filming 15 stops each, all at once and every request sent twice. 3,000
  videos, 3,000 emails, exactly one per customer, 73 FieldRoutes reads for the
  whole day. One request alone takes about 14 ms to register and 4 ms to serve
  the watch page.
- Server against hostile input (`tests/test_video_hostile.py`): garbage in
  every field, broken bodies on every endpoint, script and SQL text in names,
  comments and sign-in fields, one tech reaching for another's video, office
  pages without a session, malformed FieldRoutes records, clock changes and
  midnight. Nothing crashes, leaks or crosses over.

- The brief's match spike, on real data (read-only, Nov 10 to 12, 2026: 4 techs,
  10 tech-days, 106 stops, every one with a check-in position and a map pin).
  Ranking a tech's stops by distance from where he checked in, the true stop was
  the nearest 98% of the time and in the nearest three 99%; against only the stops
  not yet visited, 106 of 106. Both misses were a tech checking the next customer
  in while still at the previous house, not GPS error. At the house, the check-in
  position lands a median 24 m from the pin (95th percentile 57 m), and anything
  from 100 to 200 m warns on the same stops, so the 150 m "you look far" line fits
  this office. Next-door customers are rare here (one pair within 30 m in ten
  tech-days) but real, which is why GPS orders the list and never picks. The
  check-in TIME is the weak key: two techs tap in and out within a minute at a
  fifth of their stops, six stops were left checked in for over 90 minutes, and
  with 20 minutes of slack a video at the right stop also fits a neighbouring
  stop's window about half the time. A time mismatch means something; a time
  match says little.

- "One email" on the fake FieldRoutes (`tests/test_video_fr_email.py`, on SQLite
  and on Postgres). The fake models `appointment/complete` as FieldRoutes'
  help page describes it, including that the same call completes a pending
  appointment, and can be told to do what the help page says it does not (move
  the completion time, drop the comments, store them differently). Covered:
  off is the old server and never calls it; one call with two parameters and
  the technician's words kept as typed; the write sent once and never again
  without a fresh read; nothing waiting between read and write; a visit
  changed in that moment, or by the call itself, told to the office; a read
  without the comments refused; filmed first and completed later; 30 and 60;
  too late at release; FieldRoutes down for minutes, and until after the
  deadline; a write whose answer was lost; a line saved over before the email
  goes; Send it anyway, Keep it and Pull; never on a pending, cancelled,
  no-show, deleted or another customer's appointment; a line already there;
  the server killed before and after every save; two servers side by side; a
  backup put back; a row closed by the server from before this build; the
  test-customer list; several appointments, two techs, a helper; what a wait
  costs in reads.

Not proven until it runs on real phones with real accounts:

- **"One email" against the live account. None of it has run there.** Not
  proven: that `appointment/complete` on a completed appointment only updates
  the comments (no second invoice, nothing new in QuickBooks, no second email
  or text, the completion time, the technician, the times and the products
  left alone); that `appointment/get` returns a technician's typed comments
  in `notes` (all 50 that were read live were empty; the server refuses to
  write on a read without the field); that FieldRoutes stores the comments as
  they are sent (an apostrophe, an "&", line breaks, a long comment); what
  FieldRoutes' answer to the call carries; that FieldRoutes' email shows
  `{{techComments}}` with a link a thumb can tap; that the email is timed
  from the `dateCompleted` the server reads, also for a visit completed with
  no signal; what FieldRoutes' own app does to the line when a tech saves his
  comments again; that FieldRoutes sends one email per appointment; and
  whether its email goes to a customer whose email reminders are off. The
  owner's test in [`GO-LIVE.md`](GO-LIVE.md) covers all but the last two, and
  the server itself raises a flag on the first, third and fourth.

- Recording, background upload and speech-to-text on a physical iPhone. The
  simulator has no camera, so the UI tests use a stand-in recorder that writes
  a real clip; the camera and the encoder themselves have never run. Also not
  reachable in a simulator: iOS pausing the app during a background upload,
  Low Power Mode, a force-quit by the user, and how well speech recognition
  hears a name outdoors. The brief's upload spike (airplane mode, Low Power
  Mode, app swiped closed, restart) still has to be run on a device.
- The watch page on a real TV, through real AirPlay and Chromecast, in a real
  mail app on a real Android phone, and in classic Outlook. The browser tests
  use the same engines at those sizes; they are not those devices.
  [`GO-LIVE.md`](GO-LIVE.md) lists the six checks, a few minutes each.
- The TestFlight job (needs the Apple Developer account).
- Sending through Resend, the bounce webhook's payload, texts through Quo and
  push through APNs. Each is built to the provider's documentation and has
  never run with real keys.
- `review/create` against the live account. `note/create` and `task/create`
  use the same params the MCP server already verified live.
- Whether `emailReminders = 0` in FieldRoutes is a fair stand-in for "opted
  out". It is a setting; if it hides too many customers, turn it off.
- How the live bucket words a missing file, and whether it answers the list
  call the start-up check makes. Both are built to the S3 standard and fail
  safe: if the wording differs, no video is ever pulled by mistake, and the
  office gets a "cannot tell whether a video's file is there" flag instead.
  One read-only request for a made-up file name on the live bucket would
  settle it.
- The mail service's "that key is used with a different email" answer (409,
  `invalid_idempotent_request`), read as "already sent" only when no earlier
  try for that video failed. It is built to Resend's documentation and has
  never been seen from the real service. Whether Resend remembers a key from a
  request it answered with an error is not known; the server is right either
  way. One real send with a repeated key, before go-live, would show it.
- That the live bucket gives a video file an ETag on every answer, and the
  same one each time. The check for a changed file compares ETags; with none
  on an answer nothing is pulled and the email waits.
