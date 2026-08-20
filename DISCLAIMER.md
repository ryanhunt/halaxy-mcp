# Disclaimer

This project is shared as-is, for anyone who finds it useful as a starting
point. It is **not** legal, privacy, security, or compliance advice, and
using it does not make your own deployment compliant with anything.

**No warranty.** This software is provided "as is", without warranty of
any kind, express or implied, including but not limited to warranties of
merchantability, fitness for a particular purpose, and non-infringement.
See the [LICENSE](LICENSE) for the full text - GPLv3 already includes a
no-warranty disclaimer (sections 15-16); this file restates it in plain
language and adds the privacy/compliance points GPLv3 doesn't cover.

**Do your own research before deploying this anywhere real**, especially
if (like the practice this was built for) you handle health information:

- Privacy obligations (e.g. Australia's Privacy Act and Australian
  Privacy Principles, or your own jurisdiction's equivalent) are yours to
  assess and satisfy - not something this codebase decides for you.
  Health service providers are commonly *not* exempt from obligations
  that apply to small businesses generally, even if your business itself
  is small.
- Sending any data - even data this server considers "minimised" - to a
  third-party AI vendor (Claude, ChatGPT, Copilot, or any other) is a
  disclosure to that vendor's infrastructure, and needs its own lawful
  basis, appropriate vendor data-handling terms, and (where relevant)
  patient consent or a compliant privacy policy. This project makes
  design choices intended to reduce that exposure (see the "Patient data"
  section in [README.md](README.md)), but those choices don't substitute
  for actually confirming compliance with a qualified advisor.
- Review the code yourself before trusting it with real patient/practice
  data. It's a personal project, tested against one real Halaxy account
  by its author, not independently audited or professionally supported.
- You are responsible for your own Halaxy API key's scopes, your own
  server's security (OAuth login credentials, TLS, network exposure),
  and your own backups/monitoring.

**No support commitment.** This is shared in the hope it's useful, not as
a supported product. There's no SLA, no guaranteed response to issues,
and no guarantee of continued maintenance.

If any of this doesn't work for your situation, don't use it - or fork it
and adapt it, subject to the [GPLv3 license](LICENSE).
