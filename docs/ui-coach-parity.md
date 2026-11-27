# UI and Coach parity

Every action the athlete can take works directly in the UI and through the
Coach (athlete decision, #3949). This matrix lists each athlete-facing action
with its UI place and route, its Coach tool, the guard that applies and the
issue that closes a missing side. `—` marks a missing side.

Parity values: `both`; `UI only` or `Coach only` (a gap with its issue);
`neither`; `UI by rule` (the Coach must not do it: credentials, logins,
backups, purges and device permissions stay in the UI, and so do the
settings the athlete kept there on #3960).

Guards stay as they are on both sides: Coach writes keep the server-side impact
gate (receipt, whole-turn undo when every write is tracked, a confirmed request
in its own domain for deletes, archives and bulk changes, a named Intervals.icu
or Garmin sync); UI writes keep their confirmations and receipts; nothing writes
remotely without an explicit action. A gap is closed with the same backend use
case on both sides, so the guards match.

`tests/test_api_routes_doc.py` keeps this file honest: every Coach tool in
`COACH_DIALOGUE_TOOLS` and every athlete write route appears here, every named
tool and route exists, every parity value is one of the five above and matches
the two columns, and each gap names its issue. No hand-kept count: every PR that
added a row conflicted on it (#4092).

## Matrix

| Domain | Action | UI | Coach | Guard | Parity | Gap |
| --- | --- | --- | --- | --- | --- | --- |
| Profile | Read profile and competitions | Mehr › Profil `GET /api/profile` | `read_profile`, `read_coach_context` | — | both | |
| Profile | Edit profile fields, time zone, weather location, preferred sources | Mehr › Profil `PUT /api/profile` | `update_profile` | receipt, undo | both | |
| Profile | Set the energy goal | Analyse › Körper "Energieziel" `PUT /api/profile` | `update_profile` (`energy_goal`, `energy_goal_kcal`) | receipt | both | |
| Zones | Read zones and thresholds | Profil › Zonen & Schwellen `GET /api/zones` | `read_coach_context` | — | both | |
| Zones | Save a local zone configuration | Zonen & Schwellen `PUT /api/zones` | `save_zones` (`save`) | receipt, undo | both | |
| Zones | Remove a local zone configuration | Zonen & Schwellen `PUT /api/zones/remove` | `delete_zones` | delete confirmation, undo | both | |
| Zones | Adopt the first-threshold estimate | Zonen & Schwellen `POST /api/profile/first-threshold/adopt` | `save_zones` (`adopt_first_threshold`) | receipt, undo; replacing a measured value needs a confirmed request | both | |
| Zones | Adopt an LT2 estimate | Zonen & Schwellen `POST /api/zones/lt2/adopt` | `save_zones` (`adopt_lt2`) | receipt, undo; replacing an entered value needs a confirmed request | both | |
| Competitions | Create or change a competition | Kalender › Wettkampf `PUT /api/competitions` | `save_competition`, `list_competitions` | receipt, undo | both | |
| Competitions | Delete a competition | Kalender › Wettkampf `PUT /api/competitions/remove` | `delete_competition` | delete confirmation | both | |
| Competitions | Send competitions to Intervals.icu | Kalender › Wettkampf `POST /api/intervals/competitions/send` | `sync_competitions` | named sync | both | |
| Planning | Read planned units and the planning view | Kalender `GET /api/plan` | `list_planned_workouts`, `read_training_state` | — | both | |
| Planning | Create a planned unit | Bibliothek "+ Einheit" `POST /api/library`, then "Einplanen" `POST /api/library/{id}/schedule` | `apply_training_patch` (`workouts`) | receipt, undo, plan checks | both | |
| Planning | Change a planned unit's content | Kalender card "Bearbeiten" `POST /api/planned-units/{id}/update` | `apply_training_patch` (`update`) | receipt, undo, plan checks | both | |
| Planning | Move a planned unit to another day | Kalender card `POST /api/planned-units/{id}/date` | `apply_training_patch` (`update` with `date`) | receipt, undo | both | |
| Planning | Delete a planned unit | Kalender card `POST /api/planned-units/{id}/delete` | `apply_training_patch` (`delete`) | delete confirmation | both | |
| Planning | Archive or restore a planned unit | Kalender card "Archivieren" `POST /api/planned-units/{id}/archive`, receipt "Rückgängig" `POST /api/change-history/undo` | `apply_training_patch` (`archive`, `restore`) | confirmed request, undo | both | |
| Planning | Set a planned unit's focus | Kalender card `POST /api/planned-units/{id}/focus` | `apply_training_patch` (`session_focus`, `strength_intensity`) | receipt | both | |
| Planning | Choose a strength exercise's Garmin exercise | Kalender card `POST /api/planned-units/{id}/garmin-exercise` | `apply_training_patch` (`garmin_exercise`) | receipt | both | |
| Planning | Read multi-week training plans | Kalender › Pläne `GET /api/plan` | `list_training_plans` | — | both | |
| Planning | Create a multi-week training plan | Kalender › Pläne "+ Plan" `POST /api/training-plans`, its units in the unit editor | `stage_training_plan`, `commit_training_plan` | receipt, plan checks | both | |
| Planning | Replace a period of the plan | Kalender › Pläne "Zeitraum ersetzen" `POST /api/training-plans/{id}/replace`, receipt "Rückgängig" `POST /api/change-history/undo-batch` | `replace_training_plan` | confirmation, receipt, plan checks, undo | both | |
| Planning | Change, archive or delete a training plan | Kalender › Pläne `POST /api/training-plans/{id}/update`, `POST /api/training-plans/{id}/delete` | `update_training_plan` | confirmed request, undo | both | |
| Planning | Preview and apply an adaptive replan | Kalender card "Plananpassung" shows the stored preview, "Übernehmen" `POST /api/planning/replan/apply` | `preview_adaptive_replan`, `apply_adaptive_replan` | approved preview, undo | both | |
| Planning | Read the week review and training report | Analyse `GET /api/analysis/week-review` | `get_training_report` | — | both | |
| History | Undo a change | receipt "Rückgängig" `POST /api/change-history/undo`, `POST /api/change-history/undo-turn`, `POST /api/change-history/undo-batch` | `undo_training_change` | bound preview, confirmation | both | |
| History | Read the change history | Mehr › Betrieb "Verlauf der Änderungen" `GET /api/change-history` | `list_change_history` | — | both | |
| Library | List library workouts | Bibliothek `GET /api/library` | `list_workout_library` | — | both | |
| Library | Create a library workout | Bibliothek "+ Einheit" `POST /api/library` | `manage_training_templates` (`create`) | receipt, undo | both | |
| Library | Edit a library workout | Bibliothek "Bearbeiten" `POST /api/library/{id}/update` | `manage_training_templates` (`update`) | receipt, undo | both | |
| Library | Schedule a library workout on a day | Bibliothek "Einplanen" with the optional plan `POST /api/library/{id}/schedule` | `apply_workout_library_plan` | plan checks, receipt, undo | both | |
| Library | Archive a library workout | Bibliothek `POST /api/library/{id}/archive` | `manage_training_templates` (`archive`) | confirmation | both | |
| Library | Restore an archived library workout | Bibliothek › Archiviert `GET /api/library` (`archived=1`), "Wiederherstellen" `POST /api/library/{id}/restore` | `manage_training_templates` (`restore`) | receipt, undo | both | |
| Library | Delete a library workout | Bibliothek › Archiviert "Endgültig löschen" `POST /api/library/{id}/delete` | `manage_training_templates` (`delete`) | confirmed request, undo | both | |
| Activities | Read sessions and session details | Kalender `GET /api/activities`, `GET /api/activities/{id}` | `list_recent_activities`, `get_activity_details` | — | both | |
| Activities | Read analyses, trends and records | Analyse `GET /api/analysis/trends`, `GET /api/analysis/training-records` | `read_training_records` | — | both | |
| Activities | Save or edit activity feedback and purpose | Session details `POST /api/activities/{id}/feedback` | `save_activity_feedback` | receipt, undo | both | `feel` (#4199): route and Coach; its session-details control follows |
| Activities | Clear activity feedback | Session details `POST /api/activities/{id}/feedback` | `delete_activity_feedback` | receipt, undo | both | |
| Activities | Confirm, split or merge a session bundle | Session details `POST /api/activities/{id}/session` | `confirm_activity_session`, `split_activity_session`, `merge_activity_sessions` | receipt, undo | both | |
| Activities | Delete a session bundle | Session details `POST /api/activities/{id}/session` (`delete`) | `delete_activity_session` | delete confirmation, undo | both | |
| Activities | Rename a recognised route | Session details `POST /api/routes/rename` | `rename_activity_route` | receipt | both | |
| Equipment | Create, edit or retire equipment | Mehr › Ausrüstung `POST /api/equipment` | `save_equipment` | receipt | both | |
| Equipment | Record maintenance | Mehr › Ausrüstung `POST /api/equipment/maintenance` | `log_equipment_maintenance` | receipt | both | |
| Equipment | Assign equipment to a session | Session details `POST /api/equipment/assignment` | `assign_activity_equipment` | receipt | both | |
| Equipment | Assign equipment to a planned unit | Kalender card `GET /api/equipment/planned`, `POST /api/equipment/assignment` | `assign_activity_equipment` (`planned_unit_id`) | receipt | both | |
| Equipment | Set a default equipment rule | Mehr › Ausrüstung `POST /api/equipment/rules` | `set_equipment_rule` (`set`) | receipt, undo | both | |
| Equipment | Apply a rule to earlier sessions | Mehr › Ausrüstung `GET /api/equipment/rules/preview`, `POST /api/equipment/rules/apply` | `set_equipment_rule` (`preview`, `apply`) | preview count, confirmed bulk request | both | |
| Nutrition | Read the diary day, week and energy | Ernährung `GET /api/nutrition/day`, `GET /api/nutrition/week`, `GET /api/body/energy` | `read_nutrition` | — | both | |
| Nutrition | Log a saved product as a meal | Produkt dialog `POST /api/nutrition/entry` | `save_nutrition_entry` | receipt, undo | both | |
| Nutrition | Log a composed meal from several foods | Produkte "Mahlzeit zusammenstellen" `POST /api/nutrition/entry` | `save_nutrition_entry` | receipt, undo | both | |
| Nutrition | Add a saved product to a meal | Tagebuch card `POST /api/nutrition/entry/add-product` | `update_nutrition_entry` (`components`) | receipt, undo | both | |
| Nutrition | Remove, resize or swap a meal component | Tagebuch card pencil `GET /api/nutrition/entry/components`, `POST /api/nutrition/entry/components` | `update_nutrition_entry` (`components`) | receipt, undo | both | |
| Nutrition | Change a meal's date, time or type | Tagebuch card pencil `POST /api/nutrition/entry/components` (`meal`) | `update_nutrition_entry` | receipt, undo | both | |
| Nutrition | Mark a meal as estimated | Tagebuch card `POST /api/nutrition/entry/estimate` | `update_nutrition_entry` (`estimate_uncertain`) | receipt, undo | both | |
| Nutrition | Attach a session meal to its session | Tagebuch card `POST /api/nutrition/entry/session` | `update_nutrition_entry` (`session_id`) | receipt | both | |
| Nutrition | Delete a meal | Tagebuch card `POST /api/nutrition/entry/delete` | `delete_nutrition_entry` | delete confirmation, undo | both | |
| Nutrition | Create, edit or delete a saved meal | Produkte "Gespeicherte Mahlzeiten" `GET /api/nutrition/templates`, `POST /api/nutrition/templates`, `POST /api/nutrition/templates/from-entry`, `POST /api/nutrition/templates/delete` | `save_nutrition_template`, `delete_nutrition_template` | receipt, undo | both | |
| Nutrition | Log a saved meal | Produkte "Eintragen" `POST /api/nutrition/templates/log` | `log_nutrition_template` | receipt, undo | both | |
| Products | Search foods and products | Produkte `GET /api/nutrition/products`, `POST /api/nutrition/products/lookup` | `lookup_food`, `calculate_food_nutrition` | — | both | |
| Products | Save a confirmed product, also from a label photo | Produkte `POST /api/nutrition/products`, `POST /api/nutrition/products/extract` | `save_nutrition_product` | receipt | both | |
| Products | Edit a product or fill its missing values | Produkte `PUT /api/nutrition/products` | `list_incomplete_nutrition_products`, `fill_nutrition_product_gaps`, `search_web_for_product_values` | receipt | both | |
| Products | Mark a product as favourite | Produkte `POST /api/nutrition/products/favorite` | `manage_nutrition_products` (`favorite`) | receipt | both | |
| Products | Archive a product | Produkte `POST /api/nutrition/products/archive` | `manage_nutrition_products` (`archive`) | delete confirmation | both | |
| Products | Clean up unused products | Produkte `GET /api/nutrition/products/cleanup`, `POST /api/nutrition/products/cleanup` | `manage_nutrition_products` (`cleanup_preview`, `cleanup`) | preview, confirmed delete and bulk request, undo | both | |
| Products | Forget a food-database entry | Produkte `POST /api/nutrition/products/database/delete` | `manage_nutrition_products` (`forget`) | delete confirmation | both | |
| Products | Re-check products online | Produkte `POST /api/nutrition/products/database/refresh`, `POST /api/nutrition/products/refresh-all` | `refresh_nutrition_products` | receipt | both | |
| Body | Save or change weight | Körper `PUT /api/body/weight` | `save_body_entry` (`weight`) | receipt; writes Garmin and Wahoo | both | |
| Body | Save or change body fat | Körper `PUT /api/body/body-fat` | `save_body_entry` (`body_fat`) | receipt; writes Garmin | both | |
| Body | Remove weight or body fat | Körper `PUT /api/body/weight/remove`, `PUT /api/body/body-fat/remove` | `delete_body_entry` | confirmation | both | |
| Body | Save or remove a skinfold measurement | Körper `PUT /api/body/skinfolds`, `PUT /api/body/skinfolds/remove` | `save_skinfolds`, `delete_skinfolds` | receipt; writes Garmin; delete confirmation | both | |
| Check-ins | Enter or correct the daily check-in | Kalender today "Check-in" `GET /api/checkins`, `POST /api/checkins` | `save_checkin` | receipt, undo | both | |
| Check-ins | Report illness or wellness from the infection hint | Kalender day `POST /api/infection-hint/ill`, `POST /api/infection-hint/well` | `save_checkin` | receipt, undo | both | |
| Calendar | Classify an external calendar event | Kalender "Einordnen" `POST /api/external-calendar/decisions` | `classify_calendar_event` | receipt | both | |
| Syncs | Refresh calendars, Garmin, Wahoo and weather | Sync buttons `POST /api/external-calendar/sync`, `POST /api/garmin/sync`, `POST /api/wahoo/sync`, `POST /api/weather/sync`, `POST /api/analysis/refresh`, `POST /api/nutrition/refresh` | `start_provider_refresh` | — | both | |
| Syncs | Read sync state and job results | Status strip `GET /api/sync/status`, `GET /api/sync/jobs/{id}` | `get_sync_job` | — | both | |
| Syncs | Send planned units to Intervals.icu or Garmin | Kalender week sync `POST /api/sync/jobs` (`plan_push`) | `start_intervals_plan_sync`, `push_strength_to_garmin` (`push`) | named sync | both | |
| Syncs | Remove the app's own Garmin workouts of a unit | Kalender card "Aus Garmin entfernen" `POST /api/sync/jobs` (`plan_push` with `remove`) | `push_strength_to_garmin` (`remove`) | Garmin removal card | both | |
| Syncs | Resolve a training sync conflict | Kalender › Wettkämpfe card `POST /api/competitions/{id}/resolve`; a failed job runs again from its sync button | `resolve_training_sync_conflict` | confirmation for taking the remote values | both | |
| Syncs | Start a full Garmin or Wahoo resync | Anbindungen `POST /api/garmin/full-resync`, `POST /api/wahoo/full-resync` | `start_provider_refresh` (`full_resync`) | confirmation; the Coach: the athlete's own request naming the provider | both | |
| Syncs | Check a provider connection | Anbindungen `POST /api/providers/{provider}/check`, `POST /api/nutrition/yazio/check` | `check_provider_connection` | read-only | both | |
| Syncs | Switch the automatic metric export | Anbindungen `POST /api/metric-export/automatic` | — | consent per field; athlete decision #3960 | UI by rule | |
| Settings | Select model, thinking level and service tier | Coach & Modell `PUT /api/settings/model`, `PUT /api/settings/thinking-level`, `PUT /api/settings/service-tier` | — | athlete decision #3960 | UI by rule | |
| Settings | Switch the automatic Coach turns | Mehr › App `PUT /api/settings/automatic-turns` | — | athlete decision #4274 | UI by rule | |
| Settings | Switch a notification type on or off | App `POST /api/push/preferences` | — | athlete decision #3960 | UI by rule | |
| Settings | Turn GPS-track storage on or off | Datenschutz `POST /api/privacy/gps` | — | athlete decision #3960 | UI by rule | |
| Settings | Reset the Coach conversation | Coach & Modell `POST /api/chat/reset` | — | confirmation; athlete decision #3960 | UI by rule | |
| Settings | Save or clear provider credentials | Anbindungen `PUT /api/settings/credentials` | — | secrets stay server-side | UI by rule | |
| Settings | Connect or disconnect Garmin | Anbindungen `POST /api/garmin/login`, `POST /api/garmin/login/mfa`, `POST /api/garmin/disconnect` | — | credentials | UI by rule | |
| Settings | Connect or disconnect Wahoo | Anbindungen `POST /api/wahoo/connect`, `POST /api/wahoo/disconnect` | — | OAuth | UI by rule | |
| Privacy | Download or restore a backup | Datenschutz `GET /api/privacy/backup`, `POST /api/privacy/restore` | — | backup contents never reach the Coach | UI by rule | |
| Privacy | Download the privacy export | Datenschutz `GET /api/privacy/export` | — | — | UI by rule | |
| Privacy | Delete the local athlete data | Datenschutz `POST /api/privacy/delete` | — | typed confirmation | UI by rule | |
| Privacy | Delete stored GPS tracks | Datenschutz `POST /api/privacy/gps/delete` | — | typed confirmation | UI by rule | |
| Notifications | Turn notifications on or off on this device | App `POST /api/push/key`, `POST /api/push/subscribe`, `POST /api/push/unsubscribe` | — | device permission | UI by rule | |

## Outside the matrix

Coach dialogue mechanics, not athlete actions on data: `clarify_coach_request`,
`cancel_coach_request`.

Write routes that are no athlete action on data: `POST /api/login`,
`POST /api/logout`, `POST /api/chat/stream`, `POST /api/chat/cancel`,
`POST /api/coach/actions/confirm`, `POST /api/coach/actions/cancel`,
`POST /api/coach/actions/execute`, `POST /api/diagnostics/delete`,
`POST /api/logs/delete`, `POST /api/zones/threshold-offers/dismiss`.

G-14 in #3936 added interim "Dem Coach sagen" hints on the screens with a Coach-only action; the last one, on the empty nutrition diary, was removed at the athlete's request (#4265).
