# Data attribution

The MIT licence in the [LICENSE](LICENSE) file covers this project's **code**. The data shipped in
`gaff_engine/data/`, and the data this project fetches at runtime, comes from
UK public sector sources whose terms differ per dataset — the sections below
are the accurate per-dataset account, not one blanket claim. If you
redistribute this package or publish anything derived from its output, carry
these statements with it.

## HM Land Registry Price Paid Data (`data/comps`, `data/flips`, `data/comps_enriched.json`)

> Contains HM Land Registry data © Crown copyright and database right 2026.
> This data is licensed under the Open Government Licence v3.0.

Price Paid Data tracks residential property sales in England and Wales lodged
for registration. It is published by HM Land Registry.

The address fields within Price Paid Data (postcode, PAON, SAON, street,
locality, town, district, county) contain third-party intellectual property
that the OGL does not cover: they are processed against Ordnance Survey's
AddressBase Premium product, which incorporates Royal Mail's PAF® database.
Royal Mail and Ordnance Survey permit use of that address data as part of
Price Paid Data **for personal and/or non-commercial use** and **to display
for the purpose of providing residential property price information
services**. This package is free and provides residential property price
information; if you build something commercial on top of it, or use the
address data any other way, that permission is yours to re-establish with
Royal Mail (address.management@royalmail.com), not something this notice can
grant you.

## UK House Price Index (`data/hpi`)

> Contains HM Land Registry data © Crown copyright and database right 2026.
> This data is licensed under the Open Government Licence v3.0.

The UK HPI is produced by HM Land Registry with Registers of Scotland, Land &
Property Services Northern Ireland and the Office for National Statistics.

## Energy Performance of Buildings register — NOT redistributed

This package ships **no EPC certificate data**, deliberately. The register
(published by MHCLG via get-energy-performance-data.communities.gov.uk)
licenses its non-address fields under the OGL v3.0, but the address fields on
every certificate are Ordnance Survey / Royal Mail intellectual property,
licensed only for specific energy-performance purposes — and this package's
use of floor areas for £/sqft valuation is not one of them.

EPC data is therefore fetched live, with your own API token, under the terms
you accept when you register at the EPC service (see the README). Whether
your use fits those terms is between you and the register — note they permit
address data only for specific energy-performance purposes. Do not republish
your local cache.

## Open Government Licence v3.0

<https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/>

## What is deliberately NOT here

This project does not scrape property portals and ships no portal content. The
listings in `gaff_engine/fixtures/` are synthetic: they are written to the
shape of a portal payload so the parser can be tested, and are marked
`isDemo` where they stand in for real homes. Supplying listing data is the
user's own responsibility, and their own business with whoever they get it
from.
