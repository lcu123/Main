# Go-live checklist

Plain steps, in order. Each one says who does it.

## Where things stand

- The server is live at `https://zest-video-production.up.railway.app`.
  It is safe by default: **no customer gets an email** and **nothing is written
  to FieldRoutes** until the switches in step 6 are flipped.
- The iPhone app is written and compiles. It is not on any phone yet. That
  needs an Apple Developer account (step 1).

## 1. Apple Developer account — owner

Enroll at <https://developer.apple.com/programs/enroll/>. It costs $99 a year.

- Enrolling as a **company** puts "Zest Lawn & Pest" on the app. Apple asks for
  a D-U-N-S number and can take a few days to approve.
- Enrolling as an **individual** is usually approved the same day, and the app
  shows your own name. You can move to a company account later.

Nothing else on the phone side can start until this is approved.

## 2. Email sender — owner, about 15 minutes

The server sends two kinds of email: login codes, and the customer's video link.

1. Make an account at <https://resend.com>.
2. Add the domain `zestlawnpest.com`. Resend shows a few DNS records (SPF and
   DKIM). Add them where the domain's DNS is managed, then press Verify.
3. Create an API key.
4. In Railway, on the `zest-video` service, set `RESEND_API_KEY` to that key and
   delete the `VIDEO_MAIL_BACKEND` variable.
5. Set `VIDEO_MAIL_FROM` to the address the emails come from, for example
   `Zest Lawn & Pest <video@zestlawnpest.com>`, and `VIDEO_MAIL_REPLY_TO` to the
   office inbox, so customer replies land with the office.

## 3. Office sign-in — owner

In Railway, on `zest-video`, set `VIDEO_OWNER_EMAILS` to your email (more than
one: put commas between them). Then open
`https://zest-video-production.up.railway.app/office`, type that email, and
enter the code that arrives. Add office staff under **Office users**.

## 4. Put the code on the main branch — owner says go, Claude does it

All of this lives on the branch `claude/upbeat-pascal-c7ik3z`. Ask Claude to
open a pull request and merge it. The FieldRoutes connector keeps working the
same; it only gains the new files.

## 5. Put the app on phones (TestFlight) — owner gives keys, Claude runs it

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

## 6. Turn it on, in three steps — owner

| Step | Set on `zest-video` in Railway | What happens |
| --- | --- | --- |
| Try it yourself | `VIDEO_EMAIL_MODE=test`, `VIDEO_EMAIL_TEST_TO=<your email>` | Every customer email comes to you instead. |
| Save to FieldRoutes | `FR_WRITES=on` | The link is saved as a note, scores as reviews, recommendations as tasks. |
| Pilot, then everyone | `VIDEO_EMAIL_MODE=live` | Customers get their email. |

Run the brief's tests before going live: 20 videos in a row with airplane mode
on and off and none lost; one ride-along day where every video lands on the
right customer; your own account gets the email, plays the video, leaves a
score, and the score shows in FieldRoutes.

## 7. Before the App Store listing — owner

- Apple's reviewer needs a login. One is set up: the values are in Railway on
  `zest-video`, under `VIDEO_DEMO_CONTACT` and `VIDEO_DEMO_CODE`. Put them in
  the "App Review Information" box. That account sees three made-up stops, and
  its videos never reach a customer or FieldRoutes.
- Privacy policy URL: `https://zest-video-production.up.railway.app/privacy`
- Support URL: `https://zest-video-production.up.railway.app/support`
- Ask Apple for **unlisted** distribution, so only people with the link can
  install it.

## Nice to have

- **Your own web address.** `video.zestlawnpest.com` instead of the Railway
  address. Add the domain to `zest-video` in Railway, add the DNS record it
  shows, then set `VIDEO_BASE_URL` to the new address. The app needs one new
  build to point at it, so do this before the pilot if you want it.
- **Login codes by text.** Set `QUO_API_KEY` and `QUO_FROM_NUMBER`. Until then
  codes go by email only. A tech with no email on his FieldRoutes record needs
  one added, or the office can read him a code from Techs & phones.
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
- **Running cost.** Railway (server, database, storage) should be a few dollars
  a month at about 33 videos a day. Resend's free plan covers this volume.
  Apple is $99 a year.
