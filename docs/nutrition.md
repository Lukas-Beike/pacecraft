# Nutrition

The diary subtly flags complete macro values that fall outside the personal daily ranges: protein 2 g/kg current body weight with a 15 % tolerance, fat 20–30 % of the recorded calories, and carbohydrates depending on weight, training duration and intensity. From 90 minutes the range is 5–7 g/kg, from 120 minutes 6–10 g/kg, otherwise 3–5 g/kg. At least ten minutes in zone 4 or higher within a session raise the carbohydrate range by one step. Completed sessions replace the planned sessions they are matched to; a completed strength session counts with its total time, other sports with their moving time. If training or intensity data are missing, 3–5 g/kg serves as the starting range for light load; the actual need may be higher. If weight or macro values are missing, the respective comparison is omitted; low values are not flagged on the current day.

Besides calories, carbohydrates, protein and fat, the daily overview also shows consumed sugar and salt in grams. Both values are calculated from the stored product and food data with the amount eaten; later product changes do not change what was eaten. Missing values stay unknown, and known partial sums are labelled "unvollständig" (incomplete). The history shows averages for sugar and salt with the number of fully recorded days. The diary shows each composite meal as one entry; ingredients can be expanded in the card. Stored product and database snapshots keep the origin that was used, even after later product changes. Unknown nutrient values stay unknown and are not shown as zero in cards and daily totals. The entry stays local; synchronization to Intervals.icu happens only after explicit approval.

For energy expenditure, the last Garmin sync is shown compactly as **Sync: date, time**; without a sync time, an available fetch time is shown as **Abgerufen** (fetched). The ingredient sources of composite meals are listed in the expandable ingredient list.

The **Ernährung** (Nutrition) tab shows **Tagebuch** (diary), **Verlauf** (history), **Körper** and **Produkte** (products). **Verlauf** shows either 14 completed days or 12 weeks up to yesterday: the recorded calorie surplus or deficit, the average daily calorie balance, average intake and total Garmin expenditure, and average carbohydrates, protein and fat. Below the values, the chart shows intake and expenditure as separate bars with daily values or weekly averages; exact daily values can be expanded. Each average states the number of days it covers. For intake and expenditure, the weekly bars each state their own data coverage; if the coverage differs, their difference is not a balance. Expenditure stays visible even without nutrition tracking. The balance uses only days with both intake and total expenditure; with gaps it is a partial balance. Macro averages use only complete values per macro. Missing days do not count as zero; how meaningful the figures are depends on fully recorded meals. Recording and corrections go through the Coach by text or photo. A diary meal can also be deleted directly after a confirmation; a receipt offers undo. Other actions in the tab prepare an editable message and do not save or send anything automatically. An existing chat draft is kept. If you give no time, the Coach saves the entry with "Uhrzeit unbekannt" (time unknown) and the meal type; the card then shows only the type instead of an invented time.

With "Definiere mein Standardfrühstück" (define my default breakfast), reusable meals with ingredients, amounts and nutrient values for one portion can be created. The Coach saves the template directly when the request is clear, shows it in the reply with "Rückgängig", and asks only when something is unclear. A template does not yet count as eaten. "Ich habe eine halbe Portion meines Standardfrühstücks gegessen" (I ate half a portion of my default breakfast) records the intake with correspondingly scaled nutrient values. One-off deviations change only the diary entry; permanent changes change the template and never earlier entries.

The Coach prefers **BLS 4.0** for basic foods and **Open Food Facts** for branded products and barcodes; if **Yazio** is set up, it searches there first when no source is named explicitly (see "Yazio product search"). The backend code calculates calories and macros from the selected food IDs and amounts; database source, reference amount and ingredient amounts are kept in the entry and in meal templates. The tab shows them on diary entries; saved meals have no view there and are reached through the Coach. The current OFF product API is read through its structured nutrient representation; automatically added OFF estimates and statements such as "less than" are not taken over as exact label values. Raw and cooked weight are not interchangeable; grams and milliliters are not converted without a density. Ambiguous products or unknown amounts must be clarified. Without a matching result, packaging values or explicitly labelled AI estimates remain possible. Missing macros and unrecorded days are not shown as complete zero values. If individual entries lack macros, the diary shows the sum of the known values as "unvollständig" (incomplete) and states the number of affected entries; the approval preview and Coach context continue to use only complete daily totals. The synchronization sends calories and the known macros, marks the day as synchronized, and clears unknown macros on Intervals.icu (value -1) so that no earlier value remains there; if a later change supplies the missing macros, the day is synchronized again.

**Access and limits:** BLS is bundled as a local dataset and works without an API key, account or running API queries. Open Food Facts needs no API key for read access; the application identifies itself with a User-Agent. The currently documented IP limits are 15 product queries and 10 search queries per minute. The application limits itself to 14 and 9 queries per minute respectively, stores responses for at most 24 hours in the bounded in-memory cache, and pauses on 429; a 503 is reported as unavailable without a pause. Other applications behind the same public IP share the provider limits. No search on every keystroke. Only search terms/barcodes for foods are sent to Open Food Facts, no meal amounts, photos, profiles or chat histories; the provider sees the server IP. On outage or when the limit is reached, no invented database value is used.

**Data sources:** Max Rubner-Institut (2025): *Bundeslebensmittelschlüssel (BLS), Version 4.0 — Deutsche Nährstoffdatenbank*, Karlsruhe, [DOI 10.25826/Data20251217-134202-0](https://doi.org/10.25826/Data20251217-134202-0), [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/deed.de). The bundled derivative contains food name, energy, carbohydrates, protein, fat and data origin per 100 g edible portion; missing values, traces and values below the detection limit stay unknown. Reproducible extraction: `python tools/extract_bls.py <BLS_4_0_2025_DE.zip>` with the [official download](https://www.blsdb.de/download). Open Food Facts: [database under ODbL 1.0, contents under the Database Contents License](https://openfoodfacts.github.io/openfoodfacts-server/api/); community-maintained product values must be checked for product, unit and completeness. BLS data and the OFF cache stay separate. No product data or images are uploaded to Open Food Facts.

Nutrition entries stay local; a transfer of the daily totals to Intervals.icu happens only on the athlete's explicit sync request in the Coach. Templates and source information are part of the privacy export, the encrypted backup and the Nutrition deletion category.

Below the day values, the diary shows "Energieverbrauch": resting and daily expenditure from four sources, each split into rest, everyday, sport and digestion. Katch-McArdle, Cunningham and the wearable show the diary's selected day (athlete decision, #3395): that day's resting rate, everyday from its steps, sport from its sessions only (0 without one; a strength session's MET follows its session RPE, 3.5 up to RPE 5 and 0.5 more per point to 6.0 at RPE 10, and the Sport part names it, for example "Kraft RPE 8, MET 5", #3530) and digestion from its logged meals ("nicht erfasst" without any, then no day total); today a part whose input has not arrived yet (no steps, no session, no meal) counts 0 in the day total, which reads "bis jetzt", and the part reads "noch keine Daten", so early in the morning the total is the resting rate alone, while an unknown part (an unpriced session, steps without a step length) still leaves it open (athlete decision, #3494); when the day summary lags behind an already synced session (it ends before the session does, or, without its end time, fewer day steps than the session's own, or Garmin's active kcal below the session's), everyday also reads "noch keine Daten" and Garmin's day counts the session so far (#3530); while Garmin's day summary is not finished, every row's everyday names where it stands, "(Stand 12:18)", and adds ", Garmin hängt hinterher" when a session ends after it (#3530); on a past day whose summary ended before midnight, everyday is unknown and reads "noch keine Daten", so the formula rows have no day total, Garmin's day counts its sport without everyday when a session ends after the summary, and the day leaves the 28-day activity means, while its logged meals still count (#3612); today's rows read "bis jetzt", and because Garmin's resting value for today is still counting, today's wearable row shows the newest full day's resting value from the last 7 days as its rest (a day whose Garmin summary ended before midnight holds part of its resting kcal and does not count, nor in the wearable's multi-day means, #3597), labelled "Ruhe wie gestern" or "Ruhe vom" with the date, and adds today's everyday and sport so far for its day total; without one in those 7 days it reads "kein Ruhewert der letzten Tage" (#3443); a future day shows "Tag liegt in der Zukunft". "Aus deinen Daten" shows the selected day too (athlete decision, #3531): its resting rate is the own estimate from the last 28 days ("Ruhe aus 28 Tagen", the only way to derive it), its everyday, sport and digestion are the formula rows' for that day, and its day total is their sum with the resting rate's span. Verlauf opens with the card "Dein Defizit, letzte 28 Tage" (#3548), before "Welche Quelle trifft dein Gewicht?" (#3606): the energy per day from or into the body stores with its span and the goal marker, and the boxes Gewicht → Körperfett → davon Fett → pro Tag. The weight box shows the change of the fitted line, of which "davon Fett" is the fat share, so the boxes add up (#3618). The weight and the kcal per day are the 28-day window's. The body fat readings are the phase's, which set the fat share, so the Körperfett box names their own days ("07.10. → 13.11."), which can lie before the window (#3598). The weight's end is the trend line's, and says "aktuell" when it differs from the chart's last weigh-in at the shown precision. Two lines follow the boxes: "Echter Verbrauch: ≈ … kcal am Tag (Garmin Connect: …)." and "Unsicher vor allem: …." (#3606). When overlapping 28-day windows differ by more than the days they do not share can explain, as after a weight step from water or glycogen, which the weight line prices as tissue, the first reads "Geschätzter Verbrauch", the second names "Gewichtssprung (Wasser, Glykogen): die Wochen widersprechen sich", and the span of the deficit and of "Aus deinen Daten" holds that difference (#3614). The deficit span uses the same t quantile as the expenditure span. With a goal, the number and the mark take their colour from the distance to it, either way: green within 150 kcal ("✓ im Ziel"), orange up to 350 kcal, red beyond ("über Ziel" / "unter Ziel"); "Halten" counts from 0 (#3606). The card needs the weigh-ins only ("Nur Waage", #3603): without enough logged meals it keeps the deficit, its span, the boxes and the goal, and its first line reads "Echter Verbrauch: braucht erfasste Mahlzeiten.". Katch-McArdle and Cunningham take the lean mass of the body fat named under them (see below) and add the same own activity and digestion, the wearable row, named by the provider label of its daily totals ("Garmin Connect"), is the device's own split, and "Aus deinen Daten" is logged intake against the weight trend, with its band and a residual rest. How much of the weight change was fat blends a rule of thumb, 70 % ± 15 %, with the body fat measurements of the phase (athlete decision, #3555): all dated values of one method up to the window's end with a current weight (skinfolds, else entered, else a device's) give the slope of the fat mass (current weight times body fat) against the slope of the weight, each value with a repeatability of 2 percentage points. The two are weighted by their precision, so the measurements weigh more the more there are and the longer they span; two readings 14 days apart barely move the rule of thumb, weekly readings over 12 weeks give them about a quarter, over 24 weeks about three quarters. The row reads "Fettanteil: Faustregel 70 %, zu X % gemessen" ("Fettanteil: Faustregel 70 %" without at least two measurements 14 days apart), and the share's uncertainty enters the band. How the values come about sits behind the small "i", which also lists the everyday-energy inputs: steps per day, the steps sessions take out, the step length with its source, and the floors outside sessions. Everyday energy ("Alltag") counts only steps and floors, so whatever else the athlete does outside training ends up in the own row's residual: where that residual exceeds the resting reference (Katch-McArdle, else Garmin), "Aus deinen Daten" shows Ruhe at that reference, named "Ruhe wie Katch-McArdle" (or the wearable's label), and the rest as its own part "nicht erfasster Alltag" with the residual's span; its day total and every other row stay as they are (athlete decision, #3550). Learning that part for the formula rows follows later, once enough data exists. When the whole band of "Aus deinen Daten"'s resting rate lies below the resting reference (Katch-McArdle, else Garmin), it reads "nicht plausibel" with its short reason; the diary's day row then has no day total (it is built on that resting rate) but keeps its other parts, while the 28-day row in Verlauf keeps its total (#3531). A diary card can be marked "Als geschätzt markieren" when its amounts were only estimated (restaurant, friends); it then shows "Menge geschätzt", and "Nicht geschätzt" clears the mark, each with undo; the logged values never change. With such meals in the window, two rows follow the 28-day "Aus deinen Daten" row in Verlauf: "ohne geschätzte Tage" (the latest window without any marked day, named by its last day, "(bis 24.10.)", as it is older than the 28 days beside it, #3618; else "kein sauberer Zeitraum") and, once learned, "mit gelernter Korrektur"; the "i" then also names how much the estimated meals were off on average. Without enough logged days or weigh-ins, the values stay "–", and the "i" says why. Katch-McArdle and Cunningham take their body fat from the newest skinfold measurement of the last 90 days, one dated today included (the table ends yesterday); without one, from the newest body fat percentage the athlete entered in those 90 days; without one, from the newest body fat a device or scale measured in those 90 days; without any, from the profile (athlete decisions, #3362, #3402). Every body fat value saved in the profile during the last 180 days (the change history) is a dated 3-site skinfold reading of its day (athlete decision, #3557): the energy view counts it as a skinfold, "KFA: Hautfalten" with its date, and the Körperwerte body fat history labels it "Hautfalten" too, its KFA row "Lokal (Hautfalten)", an entry of that day winning (#3477, #3569). Garmin's body fat values are copies of the app's and count only on a day without an app value. Both rows name the source under their name, for example "KFA: Hautfalten", "KFA: Lokal", "KFA: Garmin Connect" or "KFA: Profil", and the Coach reads it with the values. Without any of them, both rows name the reason instead of "–" ("Körperfett fehlt", "Hautfalten älter als 90 Tage", "Geburtsjahr fehlt", "Gewicht fehlt" or "Hautfalten prüfen"), and tapping it opens the body fat dialog, the weight dialog or the profile. The Körperwerte KFA row shows only the athlete's own values (#3336, #3402). The "i" is the same small info icon as in Analyse. The Coach reads the same values with their sources.

An entry can be "Vor der Einheit" or "Während der Einheit" and carry a drink volume in ml (the card ends with " · 750 ml"). The server attaches it to a session of that day: the only one, else for "während" the session whose time window contains the entry, for "vor" the first start within 4 hours after it; a midnight-crossing session counts for both days. An ambiguous entry stays "Einheit noch nicht zugeordnet", the Coach asks which one, and the card offers the day's sessions to choose from; a choice is never changed by a later refresh. A refresh that stores a new session only fills open entries and records it in the change history. A merged session is followed on reading, a deleted or hidden one reads as unattached with the stored choice kept. A template never has a session slot; logging it may set one. Per session the app reports the carbohydrates per hour against the advised range (notable only when more than 20 % below its lower bound), ml per hour (an entry without ml, such as a gel, counts as 0 ml and the report names how many had none) and the grams before the start, as facts with unknown values left unknown. The Coach reads the same report for each session of a day it reads.

The nutrition navigation contains Tagebuch, Verlauf, Körper and Produkte. The Verlauf shows 14 completed days or 12 weeks up to yesterday, with daily values or weekly averages in the chart and an expandable table of daily values. Each day's expenditure, and so its balance, is that day's "Aus deinen Daten" total from the diary's energy table (the own resting rate of the latest 28-day window plus the day's everyday, sport and digestion), the expenditure checked against the scale, not the wearable's daily total; the card reads "Ø Verbrauch aus deinen Daten", and a day without an own total stays open (athlete decision, #3600). Missing values stay unknown; sums and averages state their data coverage. Body has moved from Analysis into the nutrition area. Verlauf shows "Welche Quelle trifft dein Gewicht?" above "Tagesverlauf" (athlete decision, #3397) for its period ("Letzte 14 Tage" or "12 Wochen"): a verdict sentence, one kcal/day scale with "Deine Daten" as a mark and a green band of its uncertainty, and each source as a dot labelled with its mean and its signed distance, nearest first; a source inside the band meets the weight. The own estimate needs at least 21 days of weigh-ins, so for 14 days it comes from the latest 4 weeks, and the note line says so, with the weeks that hold data. The verdict calls a source certain only when at least 4 windows of the last 12 weeks are shared by all sources and its lead over the next exceeds the band of "Aus deinen Daten"; otherwise it names the nearest as "noch nicht sicher". The diary's energy table outlines that source's row: solid when certain, dashed for the nearest so far. The pencil next to "Körperfett" in "Körperwerte" (or a tap on the row) opens "Körperfett eintragen", which takes a percentage or the skinfolds (#3402). Each skinfold site takes up to three readings in one row; the site counts their median, and once 10 sites have repeats the dialog shows "Deine Messgenauigkeit: ± 0,9 %-Punkte (aus 24 Wiederholungen)", which replaces the default 2 points in the fat-share blend (#3556). The pencil next to "Gewicht" enters a weight by hand with its date; the energy view counts it as that day's weigh-in, also when Garmin has a weight of the same day. The energy view takes its weigh-ins only from the app: weights entered here and the weights saved in the profile during the last 180 days (the change history), dated by the day of the save. Garmin's weights are copies of them and count only on a day without one; Wahoo's stored profile weight never counts (athlete decision, #3557). The "Produkte" tab is one list in two collapsible sections, "Favoriten (n)" and "Alle Produkte (n)" (all products, own and from a database, favorites included). Each card shows how often the product was logged, for example "42×", counted from the logged meals (a meal with several products counts each once; editing, deleting and undoing a meal update it; an own product also counts the uses of the database food with its EAN, which it hides), and beside its origin when it was last used ("zuletzt 14.11.", "zuletzt heute"), or "nie genutzt · angelegt <date>" (#3444). "Aufräumen" next to "Alle aktualisieren" removes every product not used for 30, 90 (preselected), 180 or 365 days, and every product never used, whatever its age; favorites and products a saved meal or template uses always stay. The dialog first shows the number ("23 Produkte werden gelöscht."), an expandable list with each product's last use, and the button names the count ("23 löschen"); the server finds the products again on the confirmed run. Own products are archived and database foods removed, as their single "Löschen" does, and the receipt "23 Produkte gelöscht." offers one "Rückgängig" for the whole run. Both sections list the most used first, ties by name; a search lists the best name matches first (the whole name, then its start, then the start of each word), then the most used. A product created and saved there opens "Produkt verwenden" right away, so it can be logged without a search; both are collapsed whenever the page is opened, and a search by name, brand or EAN covers all products and opens the sections with hits once; ending the search restores what was open before. Next to each total, "⚠ k" counts the incomplete entries of that section. Every card shows its origin ("eigen" with the source of its values, for example "eigen · Verpackungsangabe", or "Datenbank · Stand <local date>" for a database entry), the "⚠ fehlt:" line with the names of missing nutrients, the star for favorites, and the same four actions: "Verwenden", "Bearbeiten", "Aktualisieren" and "Löschen". "Bearbeiten" (edit) opens the editor; for a database entry only the nutrient values are editable, for an own product an EAN can be entered so that "Aktualisieren" (re-check online) works. "Alle aktualisieren" above the list re-checks every product in one background run with the same rule (empty fields fill, known and own values stay, own products record each change in the change history): own products with an EAN and stored database foods, one after another with a pause that grows after a rate limit or a source error. The button shows the progress ("Aktualisiere 12 / 80"); a second press while it runs shows the running run. At the end the status counts the products filled, unchanged, skipped without an EAN and failed; a failure does not stop the run, but 3 Open Food Facts timeouts in a row do: the status then reads "Open Food Facts antwortet gerade nicht", names how far the run got, and the products not reached keep their values (#3475). The receipt offers no undo, as database foods record no change; a filled own product is undone in the change history. The Coach has no tool for it. "Löschen" (delete) archives an own product or removes a database entry from the database, each after confirmation; meals already recorded stay unchanged. An own product counted per g or ml can store its usual "Portion" and its "Packung" (package size), each an amount in the basis unit, below Bezugsmenge/Einheit in the editor (#3452); a size in another unit is refused, and a product counted per portion takes none. The card's small line shows them after the basis ("Basis: 100 g · Portion 150 g · Packung 450 g"). The barcode lookup proposes them from Open Food Facts' serving and package quantity, and the label photo from the printed sizes; a value that is not plain g or ml, or not in the basis unit, stays empty, and the athlete checks the proposal before saving. Stored database foods take no sizes. The Coach sees the stored sizes and counts "eine Portion" or "eine Packung" of that product with them (unit `portion` or `package`); the receipt shows the amount used. "Verwenden" (use; not offered while the basis unit is unknown) offers the stored sizes as chips ("1 Portion · 150 g", "½ Packung · 225 g", "1 Packung · 450 g") that fill the amount, which stays editable; a stored portion is preselected, otherwise the amount starts at the basis amount. It asks for the amount and either adds to the Coach message (an existing draft is kept and extended by one line; nothing is sent automatically) or, directly without the Coach, adds to an existing meal of the selected day; without a selected meal, the product is recorded as a new snack. A meal extended this way becomes a composition of its previous values and the product, with the product's nutrient values fixed at the time of the addition. Coach requests remain available through the main navigation.

## Local product database (mirror)

Every Open Food Facts product that the app fetches online is automatically and
permanently stored in the local database. Since schema v27 these entries are
rows of the one product table (`nutrition_products`) with their origin (the
database, for example `open_food_facts` or `yazio`, while own products have the
origin `own`) and the database's fetch date. For an EAN that an own product holds,
the own product wins: its stored foods stay available to lookups but are not
listed separately, and a newly created own product takes only its empty fields
(brand and nutrient values in the same unit) from them, with the origin of each
filled field recorded. Lookups never change an own product. Queries first read the
own products, then this mirror, and query Open Food Facts only if
a match is missing or older than 90 days. If the online fetch is not
possible, the stored version is used with its date ("Stand vom …", as of …).
A re-check takes every value the fresh result has and keeps a stored value
where it has none, so it fills gaps such as salt or fibre and never empties a
known field; stored entries name their missing core fields (`missing_fields`).
When the athlete re-checks an Open Food Facts entry ("Aktualisieren") and core
fields are still empty, the app asks Yazio for the same EAN (only when Yazio is configured, athlete
decision 2026-11-11) and fills only those empty fields, only from a Yazio
product on the same basis; each filled field names `yazio` and its fetch time
under `filled_fields`, and a later Open Food Facts value or a change of basis
ends it. A Yazio failure is named in the status line; lookups and calculations
make no extra Yazio request.
The Coach helps complete products: `list_incomplete_nutrition_products` lists
own products and stored database foods with empty core fields (at most 50), and
`fill_nutrition_product_gaps` writes values it looked up, per the product's own
basis, into named products. Only fields that are empty at write time are
written; a known value is reported as skipped, never replaced. An invalid item
(unknown product, non-core field, negative or implausible value) is rejected
whole. Each written field records its origin (`open_food_facts`, `yazio`, `bls`
or `web`, a web value with its source page; `manual` for a value you state) and
time under `filled_fields`. A database or web origin is accepted only for a value
that a lookup or web search returned in the last hour, scaled to the product's
basis; otherwise the item is rejected. A value above the basis in grams, or with
more energy than pure fat, is rejected for every origin.
Filling two or more products is a bulk change: the Coach asks first and needs
your yes. The turn's change line in the chat lists each product, field, value and origin, and
undo of the turn empties exactly those fields again (change history entity
`food_database_entry` for stored database foods).
Missing values can also come from the web: `search_web_for_product_values`
(up to five named products) sends one separate request to the selected model's
provider with its `web_search` tool. The request carries only each
product's name, brand, EAN, basis and missing field names, never an athlete
record or message; it runs at low reasoning effort and the default service tier.
A value is returned only when it is a plausible number for a still-empty core
field (no more than the product's basis in grams, no more energy than pure fat)
and its source page is one the search read or cited; it carries origin `web` and
that page, and web text is treated as untrusted data. The Coach shows the values
before writing them with `fill_nutrition_product_gaps`, each with its own source
page (`source_urls`). If the
endpoint rejects the tool or the request fails, the result says web search is
not available and nothing is written.
For the Coach, matches carry the origin `data_origin` (`live`,
`local_database`, `local_database_stale`). In the "Produkte" tab, the stored entries are part of the
list: "Aktualisieren" (refresh) re-checks one entry online now with the same
gap-filling rule and leaves it unchanged if the database cannot be reached,
"Löschen" of a database entry (remove from database) deletes only
the mirror entry. The mirror is part of backup, export and data deletion. The upgrade to
schema v27 moves every former mirror row into the product table without loss.

`GET /api/nutrition/products?scope=all` lists own products and stored database
foods together, sorted by name, each with its `origin`, `fetched_at`, favourite
flag, `missing_fields` and `filled_fields`; `q` searches name, brand and EAN. A
food whose EAN an own product holds appears only as that product. Every entry
has the same actions: a stored food can be a favourite (kept across re-checks),
be used for a meal like an own product, and be edited. An edit changes only the
nutrient values; each edited field is marked `manual`, a re-check keeps it
instead of the database's value, and the edit is in the change history and can
be undone. "Aktualisieren" of an own product with an EAN checks the databases
again and fills only its empty fields (undoable); without an EAN it is refused
and asks for one.

## Yazio product search (optional)

Under **Mehr > Anbindungen** (More > Connections), "Verbindung prüfen" (check connection) explicitly checks the login. The status shows the last check result and its time; after a restart or a change of credentials, a new check is needed. Rejected logins can concern account or app credentials; the exact cause is not always known. On rate limiting or outage, try again later. Loading the status makes no request to Yazio.

With Yazio credentials set up (`YAZIO_*` or the Yazio card under **Mehr >
Anbindungen**, i.e. More > Connections), product searches without an explicit source use this
order: own stored products, local database (mirror), Yazio,
Open Food Facts, BLS. The next source is queried if the previous one
has no matching result (every search word in the name, known calorie value),
is unreachable, or has reached its limit; a match in an earlier
source prevents all external requests. Barcodes go through the mirror, Yazio
and Open Food Facts. Without Yazio credentials, it stays with own products and
BLS, with Open Food Facts only on explicit request; with Yazio, search terms without a source specification are therefore also sent to Yazio and Open Food Facts. The Coach can also name `yazio`
as an explicit source. When an own product's name or brand holds every search
word, also in another form or as the end of a compound ("Bio Skyr Natur" or
"Banane" for "Bananen", "Vollmilch" for "Milch", not "Bananenbrot" for
"Banane"), the Coach uses it and asks no database, unless your own message
names that database (for example "such das bei Yazio").

Yazio matches carry the source "Yazio", values per 100 g or 100 ml and, where
Yazio provides them, portion information. Like Open Food Facts matches, they are stored in the
local mirror (schema v14) and can be saved as an own
product after review. There is no diary synchronization and no
write access to Yazio. Only the account (login), search terms,
barcodes and the profile sex, if set, are sent to Yazio; credentials and tokens do not appear in logs, diagnostics, export
or Coach context, and tokens stay in memory. Requests are limited
(10 s timeout, 20 requests per minute, 24 h cache, pause on 429;
after a rejected login, no further login attempts for 15 minutes).

**Known limitations:** The interface is unofficial and undocumented
([community specification](https://yazio-community.github.io/yazio-api-specification/),
[public description](https://saganos.github.io/yazio_public_api/)); it can
change without notice or block usage. It uses the public client pair of the Yazio app,
which is built in, and requires a known app version in the
User-Agent, which you can override with `YAZIO_USER_AGENT`. Nutrient values are read as values per
gram or milliliter (search parameters: region from `YAZIO_COUNTRY`, the profile sex when it is male or female, otherwise no `sex` parameter, without effect on the nutrient values) and multiplied by 100; implausible
values stay unknown. This assumption is tested only with synthetic fixtures,
not with a real account. Check matches before saving.

## Local products and packaging photos

The nutrition library stores confirmed individual products separately from
meal templates and diary entries. A barcode searches this
local product library first and uses Open Food Facts only as a fallback.
When the athlete describes a meal with ingredients and amounts, the Coach matches
each ingredient against the stored products first and uses matching
products by their ID; the text search finds a product if every search word occurs in
name or brand (`%` and `_` count literally; words shorter than
three characters must begin a word, so that "Ei" (egg) does not find "Reisnudeln" (rice noodles); punctuation such as commas or parentheses around a word is ignored). If
this search finds nothing, it re-checks the 100 most recently changed products without
regard to case and umlauts, with plural forms and one typo
("Champignons" finds "Champignon", "zuccini" finds "Zucchini"); here too every
search word must match. Only for ingredients without a matching product does it query BLS
or Open Food Facts. The BLS search likewise tolerates plural forms,
typos and "Soße"/"Sauce"; whole words and word endings rank before
word beginnings, so that "Reis" (rice) returns rice rather than "Erdbeereis" (strawberry ice cream). If no entry contains
all search words, it returns partial matches from the known word parts
("Risottoreis" finds Reis and Risotto) and labels them as such; even then the search also returns matching
stored products, even if the external database is unreachable
or rejects the request. All ingredients of a meal are queried in one search
(at most 20 terms); a failed term leaves the
matches of the others usable. The Coach then saves the meal directly with
all components, without a prior nutrient preview; saving calculates and
states the totals. If a response reaches the step limit, the Coach says what is
done and what is not; "weiter" (continue) resumes the task with the food IDs already
found. A
match from an online source is saved locally only after review and explicit confirmation; BLS, Open Food Facts and packaging values keep their own source labels.
The model knows the source label `fddb_export`, but there is no
FDDB import workflow. A live FDDB integration and the wrapper
[itobey/fddb-exporter](https://github.com/itobey/fddb-exporter) are currently
not integrated: the wrapper exports an existing FDDB diary to
MongoDB/InfluxDB and is not a product-level lookup API. Nutrient values are always stored with
reference amount and unit. Grams and milliliters are not converted into each other without a known
density.

When a barcode matches, the product form appears directly below it. Available values for sugar, fiber and salt are taken over; missing values stay empty. Only saving creates the product locally.

A photo of the nutrition table is size-limited and transmitted only temporarily to the
provider of the selected Coach model (OpenAI or Claude). The API returns a
structured, editable proposal; image data, provider responses and
unconfirmed values are not stored in the product library. Name,
reference amount and recognized nutrient values must be reviewed before saving.
The handover of a camera photo by Android/Chromium has not yet been verified on a real
Android device.
When an intake is recorded, the server calculates the values from the local
product and stores an immutable nutrient snapshot in the diary;
later product changes do not change earlier entries.
