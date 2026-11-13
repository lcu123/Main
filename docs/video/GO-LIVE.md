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
2. In Railway, on the `zest-video` service, set `QUO_API_KEY` to that key and
   `QUO_FROM_NUMBER` to the Quo number the texts should come from, written
   like `+19165550100`.
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
