# ApplySense company registry report

Source of company names: `ApplySense_Company_List_46.pdf` (46 numbered entries). Names and order below match the PDF. Since no ATS provider or board identifier was verified, `ats_provider` and `board_identifier` are null, and `status` is `pending_verification` for all entries. Phase 1 discovery currently has no company boards to fetch.

| # | Company | ATS verification | Greenhouse board | Careers URL |
|---:|---|---|---|---|
| 1 | United Airlines | pending_verification | — | — |
| 2 | GoDaddy | pending_verification | — | — |
| 3 | Eptura | pending_verification | — | — |
| 4 | Sopra Steria | pending_verification | — | — |
| 5 | Dentsu | pending_verification | — | — |
| 6 | AMEX | pending_verification | — | — |
| 7 | PhonePe | pending_verification | — | — |
| 8 | Mercari | pending_verification | — | https://careers.mercari.com/en/jobs/ |
| 9 | Ascendion | pending_verification | — | https://ascendion.com/who-we-are/careers/ |
| 10 | Arcesium | pending_verification | — | — |
| 11 | Qualcomm | pending_verification | — | — |
| 12 | NVIDIA | pending_verification | — | — |
| 13 | Paytm | pending_verification | — | — |
| 14 | UKG | pending_verification | — | — |
| 15 | NatWest Group | pending_verification | — | — |
| 16 | IBM | pending_verification | — | — |
| 17 | Flipkart | pending_verification | — | — |
| 18 | Zeta | pending_verification | — | — |
| 19 | Gojoko Technologies | pending_verification | — | — |
| 20 | Axtria | pending_verification | — | https://www.axtria.com/axtria-careers/ |
| 21 | Samsung | pending_verification | — | — |
| 22 | Wayfair | pending_verification | — | — |
| 23 | Zepto | pending_verification | — | — |
| 24 | Kotak | pending_verification | — | — |
| 25 | MongoDB | pending_verification | — | — |
| 26 | Zoxima Solutions | pending_verification | — | — |
| 27 | MakeMyTrip | pending_verification | — | — |
| 28 | eBay | pending_verification | — | — |
| 29 | Cohesity | pending_verification | — | https://www.cohesity.com/careers/open-positions/ |
| 30 | American Express GBT | pending_verification | — | — |
| 31 | PayPal | pending_verification | — | — |
| 32 | Walmart | pending_verification | — | — |
| 33 | Thoughtworks | pending_verification | — | — |
| 34 | JPMorgan Chase | pending_verification | — | — |
| 35 | SHL | pending_verification | — | — |
| 36 | FreechargeBiz | pending_verification | — | — |
| 37 | Thoughts2Binary | pending_verification | — | — |
| 38 | Atlassian | pending_verification | — | — |
| 39 | Visa | pending_verification | — | — |
| 40 | Progress | pending_verification | — | — |
| 41 | Tech Mahindra | pending_verification | — | — |
| 42 | Tripjack | pending_verification | — | — |
| 43 | Nykaa | pending_verification | — | — |
| 44 | UiPath | pending_verification | — | — |
| 45 | YesMadam | pending_verification | — | — |
| 46 | Snapmint | pending_verification | — | — |

## Verification summary

- Registry entries: 46; unique company names: 46.
- Verified Greenhouse companies: none.
- Pending ATS verification: all 46 companies. They remain in the registry for future supported and unsupported source integrations.
- Official careers pages located: Mercari, Ascendion, Axtria, and Cohesity. These URLs are recorded above; their ATS provider/identifier is still pending because the careers page alone did not verify the ATS board.

Registry data lives in `data/company_registry.json`. Discovery only considers entries with `status: verified`, `ats_provider: greenhouse`, and a non-empty board identifier. Pending companies and identifiers are never submitted to the Greenhouse scout.
