# Getting through App Review

What Apple rejects apps for most often, where Zest Video stands on each, and
exactly what to type into App Store Connect. Guideline numbers are from Apple's
App Store Review Guidelines; check the live text before you submit, because
Apple renumbers now and then.

## The usual reasons for rejection, and our answer to each

| # | Why apps get rejected | Guideline | Where Zest Video stands |
| --- | --- | --- | --- |
| 1 | It crashes, has bugs, or a button leads nowhere | 2.1 | Compiles and its tests pass on Xcode 16 and 26. **Not yet run on a real iPhone.** Do the pilot on TestFlight before submitting; a build that has never been on a device is the most likely way to fail here. |
| 2 | The reviewer cannot get in | 2.1 | A demo sign-in exists (`VIDEO_DEMO_CONTACT` / `VIDEO_DEMO_CODE` in Railway). It shows three made-up stops, lets the reviewer record and save, and even hands him the customer's watch page. Nothing it does reaches a real customer or FieldRoutes. Put it in the review notes (text below). |
| 3 | The server is down or not ready during review | 2.1 | The server is live. Keep it running during review and do not change `VIDEO_DEMO_CODE` once submitted. |
| 4 | Placeholder text, test data, "beta" wording | 2.1, 2.3 | None in the app. The made-up screens exist only in simulator builds and cannot ship. |
| 5 | No privacy policy, or it does not match what the app does | 5.1.1 | Live at `/privacy`, linked from inside the app and from every public page. It lists video, sound, location, speech, sign-in and device details, who they are shared with, how long they are kept and how to have them deleted. |
| 6 | Permission prompts with vague reasons, or asked too early | 5.1.1 | Each prompt says in plain words what it is for. Camera and microphone are asked when the tech first opens the camera, location when he first opens his list, notifications only after his first saved video. Nothing is asked at launch. |
| 7 | Asking for more than the app needs, above all "Always" location | 5.1.1, 5.1.2 | The app asks for "While Using" only. The leave-a-stop reminder, which needs "Always", is **off** in Settings for version one. Turn it on after approval if you want it; expect Apple to ask about it then. |
| 8 | The app breaks when a permission is refused | 5.1.1 | Location refused: the list still works by hand. Speech refused: that check is skipped. Notifications refused: nothing changes. Camera or microphone refused: a plain screen explains why and offers Open Settings. |
| 9 | Recording without clear notice | 2.5.14 | Recording only starts on a tap. A thick red border, a RECORDING tag and a countdown ring stay on screen the whole time, with a sound and a buzz at start and stop. |
| 10 | Declaring background abilities it does not use | 2.5.4 | None declared. Uploads use the system's background transfer service, which needs no declaration. |
| 11 | An account you can create but cannot delete | 5.1.1(v) | Nobody creates an account in the app: sign-in is issued by the employer from FieldRoutes. The tech can sign out (removes that phone), and the privacy policy says how to have details deleted. Say so in the review notes. |
| 12 | An app meant for one company, on the public store | 3.2 | Ask for **unlisted distribution**: the app passes normal review but only people with the link can find it. See below. |
| 13 | Screenshots that do not show the real app | 2.3 | CI photographs the real app on a 6.9-inch iPhone simulator at App Store size (1320 × 2868). The current set is in [`screens/`](screens/00-contact-sheet.png); upload a few of those (Today, Record, Recording, Save this video?, My videos). The picture behind the record screens is a drawn stand-in, because a simulator has no camera. |
| 14 | Too thin to be an app ("could be a web page") | 4.2 | It needs the camera, background upload, on-device speech and location. A web page cannot do those, which is why the brief chose a native app. |
| 15 | A third-party login with no "Sign in with Apple" | 4.8 | Does not apply: there is no social or third-party login. |
| 16 | Digital goods sold outside in-app purchase | 3.1.1 | Does not apply: nothing is sold in the app. |
| 17 | Links to Support or Privacy that do not load | 2.1, 5.1.1 | `/support`, `/privacy` and `/terms` are live. Do not take the server down after submitting. |
| 18 | Works on iPhone but breaks on the reviewer's iPad | 2.1 | iPhone-only apps are still opened on an iPad. Screens scroll instead of clipping at small sizes. Worth one check on an iPad in TestFlight. |
| 19 | Export compliance left unanswered | — | Already answered in the app: it uses only standard HTTPS encryption (`ITSAppUsesNonExemptEncryption = NO`). |

## What to type in App Store Connect

**App information**

- Name: `Zest Video`
- Bundle ID: `com.zestlawnpest.video`
- Category: Business
- Age rating: answer "None" to every content question (rates 4+)
- Privacy Policy URL: `https://zest-video-production.up.railway.app/privacy`
- Support URL: `https://zest-video-production.up.railway.app/support`
- Terms: Apple's standard licence applies by default. Ours are at
  `https://zest-video-production.up.railway.app/terms`; you can paste that link
  in the description or add it as a custom licence agreement.

If you move to your own web address later (`video.zestlawnpest.com`), change
these three links too.

**Description (short and true)**

> Zest Video is for Zest Lawn & Pest technicians. After each service visit the
> technician records a 15-second video of the property: what was done, what
> was seen and what is recommended. The app sends it to our office, and the
> customer gets a private link to watch it. A work sign-in from Zest Lawn &
> Pest is required.

**App Privacy ("nutrition label")**

Answer "Yes, we collect data", then declare these. For every one: **linked to
the user: yes; used for tracking: no; purpose: App Functionality.**

| Data type | Why |
| --- | --- |
| Contact Info → Phone Number | Sign-in |
| Contact Info → Email Address | Sign-in, when a tech uses email |
| Location → Precise Location | Suggesting the stop and checking the video is for the right address |
| User Content → Photos or Videos | The service video |
| User Content → Audio Data | The technician's voice in the video |
| Usage Data → Product Interaction | The audit trail (recorded, saved, deleted) |

These are the same six the app's own privacy manifest declares
(`ios/ZestVideo/Resources/PrivacyInfo.xcprivacy`). Keep the two in step.

**App Review Information**

- Sign-in required: yes. User name: the value of `VIDEO_DEMO_CONTACT`.
  Password: the value of `VIDEO_DEMO_CODE`.
- Notes (paste this, and fill in the two values):

> This app is used by employees of Zest Lawn & Pest, a lawn and pest control
> company, to record a short video after each service visit. Accounts are
> issued by the employer; there is no sign-up in the app.
>
> DEMO SIGN-IN: on the first screen enter the mobile number
> [VIDEO_DEMO_CONTACT], tap SEND MY CODE, then enter the code
> [VIDEO_DEMO_CODE]. No text is sent for this account; the code is fixed.
>
> The demo account shows three sample stops. Tap a stop, tap THIS IS IT, tap
> the red button and record up to 15 seconds, then tap SAVE. Because the demo
> stops have no real address, the app may show one warning asking you to
> confirm the stop; tap SAVE ANYWAY. The video uploads by itself and appears
> under MY VIDEOS. Demo videos are never sent to a customer.
>
> Recording is always started by the user and is clearly shown: a red border,
> a RECORDING label and a countdown stay on screen until it stops.
>
> Location is used only while the app is open, to suggest the nearest stop and
> to check the video matches the address. Speech recognition runs on the
> device to check that the customer's name or street was said.
>
> Account deletion: users do not create accounts. A user can sign out under
> MY VIDEOS, which removes the device. Deletion requests are handled by the
> employer as described in the privacy policy.
>
> We are requesting unlisted distribution because the app is only useful to
> our own technicians.

## Unlisted distribution

The app is for Zest's own techs, so it should not show up in App Store search.

1. Submit the app for review as usual and get it approved (or have it ready
   for review).
2. Fill in Apple's request form: <https://developer.apple.com/contact/request/unlisted-app/>.
3. Once Apple switches it, anyone with the link can install it; nobody finds it
   by searching.

TestFlight needs none of this. Up to 100 internal testers can install the day
the first build uploads, and that is how the pilot should run.

## Before you press Submit

- [ ] The pilot has run on real iPhones through TestFlight, including the
      airplane-mode test.
- [ ] One check on an iPad.
- [ ] The demo sign-in works on the build you are submitting.
- [ ] The three links above load.
- [ ] Screenshots are from the current build.
- [ ] A lawyer has looked at the Terms and Privacy pages. They are written to
      match what the app does, but they are not legal advice.
