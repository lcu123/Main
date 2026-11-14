# Go-live checklist

Plain steps, in order. Each one says who does it.

## Where things stand

- The server is live at `https://zest-video-production.up.railway.app`.
  It is safe by default: **no customer gets an email** and **nothing is written
  to FieldRoutes** until the switches in step 7 are flipped.
- The iPhone app is written and compiles. It is not on any phone yet. That
  needs an Apple Developer account (step 1).

## 1. Apple Developer account — owner

Enroll at <https://developer.apple.com/programs/enroll/>. It costs $99 a year.

- Enrolling as a **company** puts "Zest Lawn & Pest" on the app. Apple asks for
  a D-U-N-S number and can take a few days to approve.
- Enrolling as an **individual** is usually approved the same day, and the app
  shows your own name. You can move to a company account later.

Nothing else on the phone side can start until this is approved.

## 2. Text messages for sign-in — owner, about 5 minutes

Techs sign in with a code sent by text to the mobile number on their
FieldRoutes employee record. Your Quo account is already approved by the
carriers to send texts, so this only needs a key.

1. In Quo, create an API key (in the workspace settings, under API).
2. In Railway, open the `zest-video` service, go to Variables, and add
   `QUO_API_KEY` with that key as its value. Do not paste the key into a chat
   or an email. (`QUO_FROM_NUMBER` is already set to the main office line; change
   it there if the codes should come from another Quo number, written like
   `+19165550100`.)
3. In FieldRoutes, make sure every tech's employee record has his mobile
   number. A tech with no number there cannot get a text. The office page
   (Techs & phones) shows who is missing one.

How it is kept safe: a code is only ever sent to the number already on a
tech's own record, never to a number someone types in. It works once, dies
after 15 minutes, and locks after 5 wrong tries. Only active technicians can
get one. The phone then stays signed in, and the office can cut off any phone
at once.

## 3. Email sender — owner, about 15 minutes

The server emails the customer's video link, and office sign-in codes.

1. Make an account at <https://resend.com>.
2. Add the domain `zestlawnpest.com`. Resend shows a few DNS records (SPF and
   DKIM). Add them where the domain's DNS is managed, then press Verify.
3. Create an API key.
4. In Railway, on the `zest-video` service, set `RESEND_API_KEY` to that key and
   delete the `VIDEO_MAIL_BACKEND` variable.
5. Set `VIDEO_MAIL_FROM` to the address the emails come from, for example
   `Zest Lawn & Pest <video@zestlawnpest.com>`, and `VIDEO_MAIL_REPLY_TO` to the
   office inbox, so customer replies land with the office.

## 4. Office sign-in — owner

In Railway, on `zest-video`, set `VIDEO_OWNER_EMAILS` to your email (more than
one: put commas between them). Then open
`https://zest-video-production.up.railway.app/office`, type that email, and
enter the code that arrives. Add office staff under **Office users**.

## 5. Put the code on the main branch — owner says go, Claude does it

All of this lives on the branch `claude/upbeat-pascal-c7ik3z`. Ask Claude to
open a pull request and merge it. The FieldRoutes connector keeps working the
same; it only gains the new files.

## 6. Put the app on phones (TestFlight) — owner gives keys, Claude runs it

After step 1 is approved:

1. In App Store Connect, create the app with bundle ID `com.zestlawnpest.video`.
2. Under Users and Access → Integrations, create an App Store Connect API key
   with the Admin role. Download the `.p8` file.
3. In GitHub → Settings → Secrets and variables → Actions, add four secrets:
   `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8` (the file's contents) and
   `APPLE_TEAM_ID`.
4. Run the **iOS** workflow with "upload to TestFlight" ticked.
5. Invite the techs in TestFlight. They install the TestFlight app, then Zest Video.

This job has never run, because it cannot without the account. Expect one or
two rounds of fixes the first time.

## Before step 7: keep the data safe — owner, about 10 minutes

Where everything is kept:

- **Videos** are in the Railway bucket, not on the server. The phone sends each
  file straight to the bucket.
- **Records** (who filmed what, each customer's link, sign-ins, settings, flags)
  are in the Railway Postgres service, which has its own volume.
- **The server itself keeps nothing.** Railway wipes it at every deploy and
  every restart, and that is fine. A test video made on the live server was
  still playing after six deploys in one night.

From this version on, the server refuses to start on Railway if the database
or the bucket settings go missing, so it can never run on storage that is
wiped. It also refuses if the bucket and the database do not belong together:
the bucket holds none of the newest videos the database knows (the wrong
bucket), or the bucket holds videos while the database knows of none and no
phone has ever signed in on it (a new or an emptied database). What that looks
like: the deploy shows as failed, its log says "zest-video is NOT starting."
once, in plain words, and names the variable to look at, and the version that
was running before keeps running. Put the variable right and deploy again.

**The two states of today's live server that this version will not start
on.** The real program was started on every state the live database can be in
today (it has only ever held test and demo videos):

| The live database | The bucket | This version |
| --- | --- | --- |
| Has videos (demo ones, or test ones) | Still holds at least one of the newest test videos, or there are fewer than three test videos that are not demo ones | Starts. |
| Has no video at all | Empty | Starts. |
| Has no video at all, but a phone has signed in on it (a tech's, or the App Review account) | Old test files still in it | Starts, and its log warns that the bucket holds files the database does not know. |
| Has no video at all, and no phone has ever signed in on it | Old test files still in it | **Does not start.** |
| Has three or more test videos that are not demo ones | Every one of their files was cleared out by hand | **Does not start.** |

In both "does not start" rows the deploy fails, the version before keeps
serving, and the log says why once and names the way past it.

- The fourth row is a database that was emptied or made new while test files
  stayed in the bucket. The server cannot tell that from a lost database, and
  on a lost database every customer's link is dead, so it stops and says so.
  If that is the live server's state on the day this is pushed: set
  `VIDEO_START_WITH_NEW_DATABASE=1` on `zest-video` for that one deploy, and
  take it out again once a phone has signed in. Or empty the `videos/` folder
  of the bucket first.
- The fifth row looks to the server exactly like the wrong bucket. If the test
  files really were cleared out on purpose: `VIDEO_START_WITH_FILES_MISSING=1`
  for that one deploy, and take it out again the same day.

As far as is known the live database holds demo videos and a test video whose
file is still there, which is the first row.

The same message names a variable that starts the server anyway
(`VIDEO_START_WITH_FILES_MISSING`, `VIDEO_START_WITH_NEW_DATABASE`,
`VIDEO_ALLOW_THROWAWAY_STORAGE`). Each switches one of these checks off for as
long as it is set. If you ever use one, take it out again the same day. While
one is set, the first line of the log names it at every start and a warning
says to remove it.

What that does not protect is the Postgres volume and the bucket themselves. Do
these once, before real customers' videos are in there:

1. **Turn on database backups.** In Railway, open the **Postgres** service, go
   to its **Backups** tab, and switch on **Daily** and **Weekly**. Railway keeps
   daily backups for 6 days and weekly ones for 27 days. It bills them like
   volume space: cents a month at this size.
2. **Know how to bring one back, and do it in this order.** A restore puts the
   database back to the moment of the backup, and the server then acts on what
   was true at that moment. Done carelessly it emails customers a second time.
   Do it in the evening, when no tech is filming.
   1. On `zest-video`, set `VIDEO_EMAIL_MODE=off` and `FR_WRITES=off` and wait
      for the deploy to finish. (Both are off today. After step 7 they will
      not be.) Write down what they were set to.
   2. On the **Postgres** service, **Backups** tab: find the backup by its
      date, press **Restore**, then press **Deploy** on the change Railway
      shows at the top of the project.
   3. Open the office page and look at the day of the backup and the days
      since. See "What a restore undoes" below, and put right what matters.
   4. Set the two variables back to what they were.

   Try one restore before the pilot, while nothing real is in there.

   **What a restore undoes.** Everything that happened after the backup:
   - Videos filmed after it are gone from the list. Their files are still in
     the bucket, but their links are dead and cannot be brought back.
   - A video the office **pulled** after the backup is back. Pull it again.
   - A customer who asked for **no more video emails** after the backup will
     get them again. Before the restore, open the office's Audit trail page,
     type `email_opt_out` in the Action box, and note the customers listed
     since the backup's date. Afterwards ask Claude to put them back; there
     is no button for it yet.
   - A video that was still inside its 10 minutes at the moment of the backup
     will read "customer emails are off". It was emailed once already, before
     the restore. Leave it.
   - If step 1 was skipped, the server has a second guard: a video whose email
     is more than 6 hours late is not sent by itself. It waits, with a flag
     that reads "Its email was 2 days late". The same for a video that was
     waiting for the office at the backup because its visit was off the day.
     The server cannot know which of these were already emailed before the
     restore: its record of that went with the restore. So the flag says "This
     server has no record of an email for it", and tells you to look for the
     video note on the customer's account in FieldRoutes first. FieldRoutes is
     not touched by a restore, so a note there means the customer already has
     the video: do **not** press Send it anyway, that would be the second
     email. Clear the flag and leave the video. (If FieldRoutes writes were
     off at the time, no note was written. Then go by the day: a video from
     days before the restore was most likely emailed on its day.) Pull again
     the ones the office had pulled. The guard does not cover a backup
     less than 6 hours old: restored with email on, that one does send the
     same email twice and writes the same FieldRoutes note twice. So do step 1.
   - Scores, flags the office cleared, settings changed and office users
     added after the backup are as they were at the backup.
   - Phones signed in after the backup sign in again.
3. **Do not delete or rename** the Postgres service, its volume, the bucket, or
   the `VIDEO_DATABASE_URL` and `VIDEO_S3_...` variables on `zest-video`.
   Wiping a volume deletes its backups too. A deleted bucket can be brought
   back from the project's Activity feed for 52 hours; after that its videos
   are gone for good.
4. **Nice to have: a free uptime check.** Point an uptime checker (UptimeRobot
   is one) at `https://zest-video-production.up.railway.app/healthz`. That
   page says `ok` only while the server and its database are both working, so
   you get an email the minute either is down. Railway itself only looks at it
   during a deploy.

Railway has no backups for buckets. If one video's file is ever lost, the
server notices the first time the file is needed, takes that one video down and
flags the office ("Video file changed or missing"). It does that only when the
storage itself says the file is gone; on an unclear answer it takes nothing
down. Whether to keep a second copy of every video is under "Choices waiting
for you".

What the server deletes by itself. It checks once a day at 3 a.m., and at every
start:

| What | When |
| --- | --- |
| A video and its file | After the keep time under Settings (12 months unless you change it). The customer's link then says the video is no longer available. |
| The file of a video the tech deleted or recorded over | At once. |
| The file of a video the office pulled, or a customer reported as not their home | 30 days later. Until then the office can move it to the right customer. |
| The App Review account's sample videos | After a day. |
| A video that was registered and never arrived | After 15 days. |
| Sign-in codes | After 2 days. |
| The saved list of a day's stops | After 120 days. |
| The audit trail | After 2 years. |

## 7. Turn it on, in three steps — owner

| Step | Set on `zest-video` in Railway | What happens |
| --- | --- | --- |
| Try it yourself | `VIDEO_EMAIL_MODE=test`, `VIDEO_EMAIL_TEST_TO=<your email>` | Every customer email comes to you instead. |
| Save to FieldRoutes | `FR_WRITES=on` | The link is saved as a note, scores as reviews, recommendations as tasks. |
| Pilot, then everyone | `VIDEO_EMAIL_MODE=live` | Customers get their email. |

Run the brief's tests before going live: 20 videos in a row with airplane mode
on and off and none lost; one ride-along day where every video lands on the
right customer; your own account gets the email, plays the video, leaves a
score, and the score shows in FieldRoutes.

### Six checks only a real phone and a real TV can make

The simulator and the browser tests cannot see these. Each takes a few minutes
with the first real video a tech records. None needs any code.

1. **The file itself.** After the first real videos arrive, look at the office
   page. There should be no flag reading "Video may not play for every
   customer". The server reads each file as it lands and raises that flag by
   itself if a phone ever sends a format (HEVC) that many Windows and older
   Android browsers show as a black picture.
2. **Leave it open.** Open a watch link on an iPhone and in the Gmail app on an
   Android phone, leave the page 16 minutes, then press play. It should play.
   (Chrome keeps an old link and the page repairs it by itself; this confirms
   the repair on real phones.)
3. **A TV.** Open a watch link in a smart TV's own browser and press OK on the
   player. Then AirPlay it from an iPhone and Cast it from Chrome. The picture
   should be upright each time. If it lies on its side on any of them, tell
   Claude which: the phone app can turn the picture before it uploads.
4. **Classic Outlook on Windows.** Open the video email there. The button
   should be a purple bar with white words.
5. **Weak signal.** Play a video on one bar of signal. It may stop and start;
   it should say so ("Your signal is weak...") and carry on by itself. If real
   videos stutter for many customers, the recording quality can be lowered a
   little; that is your call, after seeing real footage.
6. **No signal.** Open a watch link on an iPhone, switch on airplane mode, then
   press play. After about half a minute a yellow button should be on the
   screen: "The video did not load. Press here to try again." Press it twice;
   the page should stay. Switch airplane mode off: the video should start by
   itself. Do the same once with the phone on its side.

## 8. Submit to Apple — owner

Follow [`APP-REVIEW.md`](APP-REVIEW.md). It lists the usual reasons Apple
rejects apps and where this one stands on each, and gives the exact text for
every box in App Store Connect: the description, the privacy answers, the demo
sign-in for Apple's reviewer, and how to ask for an unlisted listing.

The three links Apple asks for are live now:

- Privacy: `https://zest-video-production.up.railway.app/privacy`
- Terms: `https://zest-video-production.up.railway.app/terms`
- Support: `https://zest-video-production.up.railway.app/support`

## Nice to have

- **Your own web address.** `video.zestlawnpest.com` instead of the Railway
  address. Add the domain to `zest-video` in Railway, add the DNS record it
  shows, then set `VIDEO_BASE_URL` to the new address. The app needs one new
  build to point at it, so do this before the pilot if you want it.
- **A code read out by the office.** If a tech cannot get a text, the office
  can make a code for him under Techs & phones and read it to him.
- **The leave-a-stop reminder.** Off for now, because it needs "Always"
  location and Apple questions that. Turn it on under Settings after approval.
- **Push nudges.** Set the four `APNS_` variables from the Apple account. Until
  then the phone still reminds the tech by itself.

## Choices waiting for you

Every one of these works today, in the way the first sentence says. Each was a
judgement call made on the safe side while the server was built. Say which you
want changed. Where a choice already has a switch, it is named at the end.

When the office changes the route during the day:

1. **A video for a visit the office took off the day** (cancelled, a no-show,
   moved to another day). Today the video is kept and waits for the office to
   press Send it anyway or Pull, and nobody is emailed. The other choice: the
   email goes out as it used to, and the office is only told afterwards.
   [Settings page, the tick box "hold the email until the office says send";
   `hold_off_route`]
2. **Two videos for one visit.** When a stop is given to another tech and both
   techs film it, today the customer gets one email and the second video waits
   for the office. The other choice: both go out. [the same tick box]
3. **FieldRoutes does not answer at the moment an email is due.** Today the
   email waits a few minutes until the day's list can be checked. The other
   choice: it goes on the last list the server saved, and now and then a
   customer is emailed about a visit that had just been cancelled. [the same
   tick box]
4. **A stop given to another tech after the first tech filmed it.** Today the
   first tech's video goes to the customer, the office gets a flag, and the new
   tech is not asked for a second one. The other choice: such a video waits for
   the office too. [no switch yet]
5. **A visit that was filmed and then moved to another day.** Today the app
   asks the tech to film it again on the new day, and the office gets a flag
   saying it was already filmed. The other choice: the first video counts and
   he is not asked again. [no switch yet]
6. **The office clears the flag on a waiting video without pressing Send or
   Pull, and the visit then comes back.** Today the video keeps waiting and a
   new flag says so. The other choice: it goes out by itself. [no switch yet]
7. **A tech whose video is waiting for the office.** Today he cannot delete it
   or record over it; his phone says SENT TO OFFICE and tells him to call the
   office. The other choice: he may record a new one in its place. [no switch
   yet]
8. **A lead tech whose helper already filmed the stop.** Today the lead's phone
   shows the stop as filmed, so he may skip his own video. The other choice:
   the lead always films. [no switch yet]
9. **A helper's video that is still on his phone.** Today it counts for the
   lead from the moment it is recorded, before it has uploaded. A video left by
   a tech who had the stop before counts only once it has uploaded. The other
   choice: the same rule for helpers. [no switch yet]
10. **How many flags a route change raises.** Today each change to a filmed stop
    gets a flag of its own: handed to another tech, cancelled, moved to another
    day. The other choice: fewer flags, if the office moves stops often and
    this is too much to read. [no switch yet]
11. **Work closed out in FieldRoutes after hours.** Today it is checked at 3
    a.m. the next morning and a stop with no video is flagged at once, with no
    one-hour wait. The other choice: leave late close-outs unflagged. [no
    switch yet]

When the server was down, or a backup is put back:

12. **An email that is hours late.** Today a video whose email is more than 6
    hours late waits for the office (Send it anyway, or Pull) and does not go
    out at whatever hour the server came back. The other choice: a different
    number of hours, or 0, which lets late emails go by themselves as they used
    to. [Settings page, "Overdue hours"; `overdue_hours`]
13. **A video that waited for the route and is hours late when its visit comes
    back.** Today it does not go by itself any more: it waits for the office
    like any late video. So nobody is emailed at 3 a.m. because a visit was
    un-cancelled in the evening, and a backup put back does not send it a
    second time. Inside the 6 hours it still goes by itself. The other choice:
    it goes whenever the visit is back, however late. [the same setting; 0
    gives the other choice]
14. **Send it anyway on a late video, when the visit was cancelled meanwhile.**
    Today that press answers only what the flag said. If the flag said just
    "late", the route is still checked: a visit cancelled since, or a second
    video for the visit, holds the video once more under its own flag, and the
    office presses again if it still wants it sent. The other choice: one
    press sends it whatever the route says. [no switch yet]
15. **After a backup is put back, the server does not know who was already
    emailed.** Today the flag on a late video says so and tells the office to
    look for the video note in FieldRoutes before pressing Send it anyway; the
    server does not look itself. The other choice: have the server read the
    account's notes and refuse to send when the note is there, which costs one
    FieldRoutes read for each such video and is not built. [not built]
16. **A FieldRoutes note that was being saved when the server stopped.** Today
    it is not tried again, so nothing is ever written twice; the office gets a
    flag with the words to add by hand. The other choice: try again, and accept
    that now and then a note shows up twice on an account. [no switch yet]
17. **The storage gives an answer that proves nothing** (a "not found" that is
    not the storage's own, a page of words where a file should be, an answer
    with no fingerprint of the file on it). Today no video is taken down on
    that; its email waits and is tried again every 5 minutes, and the office
    gets a flag after half an hour. The other choice: send the email anyway
    and let the customer find out whether it plays. [no switch yet]
18. **The mail service says it already has an email for a video, after a try
    that failed.** Today the server stops trying, the record says no email is
    known to have gone, and the office gets a flag to check with the customer.
    If no try had failed (the server was only stopped at the wrong moment),
    the same answer is read as sent. The other choice: always read it as sent.
    [no switch yet]
19. **A second copy of the videos.** Today there is one copy; Railway does not
    back up its storage. The other choice: a nightly copy to a second place,
    about a dollar a month at Zest's size, which is not built yet. The database
    backup matters more: without the records the files cannot be matched to
    customers at all. [not built]

How the server starts:

20. **When the server refuses to start.** Today, on Railway, it will not start
    without its database and its storage settings, on the wrong storage, or on
    a new database beside storage that already holds videos; the version
    that was running keeps running and the log says why in plain words. The
    other choice, for each check: a named switch that starts it anyway, to be
    taken out again the same day. [`VIDEO_ALLOW_THROWAWAY_STORAGE`,
    `VIDEO_START_WITH_FILES_MISSING`, `VIDEO_START_WITH_NEW_DATABASE`]
21. **What counts as a new database.** Today: no video in it, and no phone has
    ever signed in on it. A database with no video that phones have signed in
    on is taken to be the one in use, with old files in the storage beside it,
    and the server starts with a warning; refusing would take a working server
    down at its next restart. The other choice: refuse whenever the database
    has no video and the storage holds files, sign-ins or not.
    [`VIDEO_START_WITH_NEW_DATABASE`]
22. **The first few videos and the wrong storage.** Today the check that stops
    the server starting on the wrong storage needs three videos to be sure, so
    in the first days of the pilot a wrong setting only prints a warning and
    those one or two videos would be taken down and flagged. The other choice:
    refuse on a single missing file, at the price that one test video deleted
    by hand stops a deploy. Until this is decided, do not touch the storage
    variables in those first days. [`VIDEO_S3_...`]
23. **The switch for FieldRoutes writes, if it is ever deleted.** Today a
    missing switch means writes are ON, because that is the default it shares
    with the FieldRoutes connector; the server runs with it set to off, and the
    first line of its log says on or off at every start. The other choice: for
    the video server, missing means off. [`FR_WRITES`]
24. **The same switch, mistyped.** Today any word the server does not know
    (false, 0, no, a typo) means off, here and in the connector alike, and a
    warning says so. It used to mean on in the video server. The other choice:
    the old way. [`FR_WRITES`]
25. **The secret that protects the office page's forms.** Today a missing one
    only prints a warning at start. The other choice: refuse to start without
    it. If it is ever changed, an office form that was already open fails once
    and works after a refresh. [`VIDEO_SECRET`]

## Still yours to decide

- **People and voices on camera.** The brief's default: film the property and
  the tech's own voice; keep customers, kids and neighbors out of frame. Ask a
  lawyer about California recording rules. This is not legal advice.
- **Which services get a video.** Default is every finished visit. Weekly
  services can be switched off under Settings so customers are not emailed
  every week.
- **Who gets the email on property-managed and commercial accounts.** Today it
  is the email on the customer record.
- **Running cost.** At Zest's size (about 33 videos a day): Railway for the
  server, database and storage is a few dollars a month, and the email
  service's free plan covers the volume. Apple is $99 a year. For scale, a
  company with 15 techs and 225 videos a day would run about $40 to $50 a
  month: storage about $6 once a full year of videos is kept (225 × 5 MB a
  day), server and database about $15, email about $20.
