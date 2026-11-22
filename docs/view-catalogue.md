# View catalogue

Every route of the PWA's hash router, with the panel it shows, the navigation
item it belongs to and its view contract, plus the state texts and the
confirmation texts the views show. Everything between the markers is generated
by `node tests/navigation-state.test.cjs --write`; a test per section fails when
it is out of date. An unknown route opens the default route.

## Views

The router's tables in `public/navigation.js` (`NAV_ROUTES`, `NAV_LINK_ROUTES`,
the segment lists and `ROUTE_ALIASES`) give the routes. `NAV_VIEWS` holds one
descriptor per route, for documentation only: `Loads` are the API reads the
view relies on, the areas of the start load (chat, plan, library, performance,
feedback, profile) included; the zones of Profil and the GPS tracks of Betrieb &
Diagnose are read when their section is opened. `Actions` the German labels
of its main controls as rendered, and `States` the states it can show
(`loading`, `empty`, `error`, `notConfigured`, `offline`). A test fails when a
route has no descriptor, a descriptor names no route, or a read path or control
label appears in none of the public sources.

<!-- BEGIN GENERATED VIEWS -->
Default route: `#coach`. Navigation items: `#coach`, `#plan`, `#nutrition`, `#analysis`, `#more`.

| Route | Panel | Navigation item | Loads | Actions | States |
| --- | --- | --- | --- | --- | --- |
| `#analysis` | `dataPanel` | `analysis` | `/api/performance`, `/api/analysis/power-profiles` | Analyse aktualisieren, Jetzt abgleichen, Anbindung einrichten | loading, empty, error, notConfigured, offline |
| `#analysis/cycling` | `dataPanel` | `analysis` | `/api/performance`, `/api/analysis/power-profiles` | Analyse aktualisieren, Jetzt abgleichen, Anbindung einrichten | loading, empty, error, notConfigured, offline |
| `#analysis/load` | `dataPanel` | `analysis` | `/api/performance` | Analyse aktualisieren, Jetzt abgleichen, Anbindung einrichten, 4 Wochen, 3 Monate, 6 Monate, 12 Monate, 3 Monate anzeigen | loading, empty, error, notConfigured, offline |
| `#analysis/recovery` | `dataPanel` | `analysis` | `/api/performance` | Analyse aktualisieren, Jetzt abgleichen, Anbindung einrichten, 4 Wochen, 3 Monate, 6 Monate, 12 Monate, 3 Monate anzeigen | loading, empty, error, notConfigured, offline |
| `#analysis/running` | `dataPanel` | `analysis` | `/api/performance`, `/api/analysis/power-profiles` | Analyse aktualisieren, Jetzt abgleichen, Anbindung einrichten | loading, empty, error, notConfigured, offline |
| `#analysis/strength` | `dataPanel` | `analysis` | `/api/performance`, `/api/analysis/strength` | Analyse aktualisieren, Jetzt abgleichen, Anbindung einrichten | loading, empty, error, notConfigured, offline |
| `#coach` | `chatPanel` | `coach` | `/api/chat/history?limit=100` | Senden, Morgen-Check-in, Analyse & Plan, Antwort stoppen, Weitere Nachrichten laden, Coach fragen | loading, empty, error, notConfigured, offline |
| `#more` | `profilePanel` | `more` | `/api/context-preview`, `/api/logs?limit=250`, `/api/profile`, `/api/zones` | Profil speichern, Lokal speichern, Übernehmen | empty, offline |
| `#more/app` | `settingsPanel` | `more` | `/api/context-preview`, `/api/logs?limit=250`, `/api/push` | System, Hell, Dunkel, Benachrichtigungen aktivieren | loading, offline |
| `#more/coach` | `settingsPanel` | `more` | `/api/context-preview`, `/api/logs?limit=250` | Chat zurücksetzen, Kontext laden | loading, error, offline |
| `#more/connections` | `settingsPanel` | `more` | `/api/context-preview`, `/api/logs?limit=250`, `/api/providers/connections`, `/api/nutrition/yazio/status` | Verbindung prüfen, Mit Garmin anmelden, Garmin trennen, Wahoo verbinden, Synchronisieren, Speichern, Entfernen | loading, error, notConfigured, offline |
| `#more/equipment` | `settingsPanel` | `more` | `/api/context-preview`, `/api/logs?limit=250`, `/api/analysis/training-records` | Fahrrad, Laufschuhe, Wartung erledigt, Rückgängig | loading, empty, error, offline |
| `#more/operations` | `settingsPanel` | `more` | `/api/context-preview`, `/api/logs?limit=250`, `/api/privacy/gps` | Logs herunterladen, Daten exportieren, Lokale Daten löschen, Backup herunterladen, Backup wiederherstellen, Diagnose herunterladen, Diagnose löschen, Abmelden | loading, empty, error, offline |
| `#more/profile` | `profilePanel` | `more` | `/api/context-preview`, `/api/logs?limit=250`, `/api/profile`, `/api/zones` | Profil speichern, Lokal speichern, Übernehmen | empty, offline |
| `#nutrition` | `nutritionPanel` | `nutrition` | `/api/nutrition/day`, `/api/body/energy` | Ernährung aktualisieren, Vorheriger Tag, Nächster Tag, Heute, Als geschätzt markieren, Nicht geschätzt, Löschen, Rückgängig, Erneut laden | loading, empty, error, offline |
| `#nutrition/body` | `nutritionPanel` | `nutrition` | `/api/performance`, `/api/body/skinfolds`, `/api/body/energy` | Ernährung aktualisieren, 4 Wochen, 3 Monate, 6 Monate, 12 Monate, Gewicht eintragen, Körperfett eintragen, Prozent, Hautfalten, Speichern, Löschen, Abbrechen | loading, empty, error, offline |
| `#nutrition/diary` | `nutritionPanel` | `nutrition` | `/api/nutrition/day`, `/api/body/energy` | Ernährung aktualisieren, Vorheriger Tag, Nächster Tag, Heute, Als geschätzt markieren, Nicht geschätzt, Löschen, Rückgängig, Erneut laden | loading, empty, error, offline |
| `#nutrition/products` | `nutritionPanel` | `nutrition` | `/api/nutrition/products?scope=all`, `/api/nutrition/products/refresh-all` | Barcode scannen, Nährwerttabelle fotografieren, Produkt manuell erfassen, Suchen, Alle aktualisieren, Aufräumen | loading, empty, error, offline |
| `#nutrition/week` | `nutritionPanel` | `nutrition` | `/api/nutrition/week` | Ernährung aktualisieren, 4 Wochen, 3 Monate, 6 Monate, 12 Monate, Tageswerte anzeigen | loading, empty, error, offline |
| `#plan` | `workoutsPanel` | `plan` | `/api/plan`, `/api/library?limit=100`, `/api/activities?limit=100`, `/api/analysis/week-review` | Kalender aktualisieren, Woche, Monat, Woche synchronisieren, Verschieben, Feedback, Mit Coach besprechen, Mit Coach besprechen | loading, empty, error, notConfigured, offline |
| `#plan/library` | `workoutsPanel` | `plan` | `/api/plan`, `/api/library?limit=100` | Kalender aktualisieren, Weitere Bibliothekseinheiten laden, Erneut senden | loading, empty, error, offline |
| `#plan/overview` | `workoutsPanel` | `plan` | `/api/plan`, `/api/library?limit=100`, `/api/activities?limit=100`, `/api/analysis/week-review` | Kalender aktualisieren, Woche, Monat, Woche synchronisieren, Verschieben, Feedback, Mit Coach besprechen, Mit Coach besprechen | loading, empty, error, notConfigured, offline |
| `#plan/season` | `workoutsPanel` | `plan` | `/api/plan`, `/api/library?limit=100`, `/api/profile` | Kalender aktualisieren, Erneut laden | loading, empty, error, offline |

Kalender segments: `overview`, `library`, `season`.
Mehr segments: `profile`, `connections`, `equipment`, `coach`, `app`, `operations`.

Former routes that open another view:

- `#analysis/performance` opens `#analysis/running`
- `#analysis/records` opens `#analysis/running`
- `#analysis/sessions` opens `#plan/overview`
- `#analysis/week` opens `#plan/overview`
- `#more/appearance` opens `#more/app`
<!-- END GENERATED VIEWS -->

## State texts

Every call of the state helpers of `public/components.js` (`createEmptyState`,
`createLoadingState`, `createErrorState` with its fixed heading and retry
button, `markValueMissing`) and every paragraph drawn with an empty-state class
(`h(.empty)`, `h(.context-empty)`) with its text arguments, per file and
function. A text that is not a literal is listed as `(computed)` with its
expression. Other inline status lines are not listed.

<!-- BEGIN GENERATED STATES -->
### `public/activity-analysis.js`

| Function | Helper | Text | Detail |
| --- | --- | --- | --- |
| `loadPanel` | `createLoadingState` | Details werden geladen … |  |
| `loadPanel` | `createErrorState` | (computed) ` error.message ` |  |

### `public/analysis.js`

| Function | Helper | Text | Detail |
| --- | --- | --- | --- |
| `analysisChart` | `h(.empty)` | Ein einzelner Messwert zeigt noch keinen Verlauf; mindestens zwei datierte Werte sind erforderlich. |  |
| `cyclingPowerProfileTable` | `markValueMissing` | nicht erfasst |  |
| `showProfileCardsState` | `createErrorState` | (computed) ` error.message ` |  |
| `showProfileCardsState` | `createLoadingState` | (computed) `` `${title} wird geladen…` `` |  |
| `renderStrengthAnalysis` | `createLoadingState` | Kraftdaten werden geladen… |  |
| `renderStrengthAnalysis` | `createErrorState` | (computed) ` error.message ` |  |
| `renderAnalysisTrends` | `createErrorState` | (computed) ` error.message ` |  |
| `renderActivityRecords` | `markValueMissing` | – |  |
| `renderRunningBestEfforts` | `markValueMissing` | – |  |
| `energyPending` | `h(.empty)` | Tag liegt in der Zukunft |  |
| `energyPending` | `createLoadingState` | Energieverbrauch wird geladen… |  |
| `energyPending` | `h(.empty)` | Energieverbrauch konnte nicht geladen werden. |  |
| `renderBodyAnalysis` | `h(.empty)` | Noch keine Körper-Historie verfügbar. |  |
| `renderTrainingRecords` | `createLoadingState` | Ausrüstung wird geladen… |  |
| `renderTrainingRecords` | `createErrorState` | (computed) ` error?.message ` |  |
| `analysisEmptyCard` | `createEmptyState` | (computed) ` ANALYSIS_EMPTY_TITLES[segment] ` | (computed) ` cause ` |

### `public/coach.js`

| Function | Helper | Text | Detail |
| --- | --- | --- | --- |
| `renderMessages` | `createEmptyState` | Dein Coach ist bereit | – |

### `public/nutrition.js`

| Function | Helper | Text | Detail |
| --- | --- | --- | --- |
| `nutritionTotalTile` | `markValueMissing` | nicht erfasst |  |
| `nutritionStatTile` | `markValueMissing` | nicht erfasst |  |
| `nutritionBalanceCard` | `markValueMissing` | nicht erfasst |  |

### `public/performance-view.js`

| Function | Helper | Text | Detail |
| --- | --- | --- | --- |
| `renderCurrentPerformance` | `h(.empty)` | (computed) ` text ` |  |
| `renderPerformanceLoading` | `createLoadingState` | Werte werden geladen… |  |
| `renderPerformanceLoading` | `createLoadingState` | Prognosen werden geladen… |  |

### `public/plan-views.js`

| Function | Helper | Text | Detail |
| --- | --- | --- | --- |
| `plannedWeekReview` | `createLoadingState` | Wochenrückblick wird geladen… |  |
| `weekReviewFailed` | `h(.empty)` | Wochenrückblick gerade nicht verfügbar |  |
| `retryWeekReview` | `createLoadingState` | Wochenrückblick wird geladen… |  |
| `renderLibrary` | `h(.context-empty)` | Noch keine Einheiten in der Bibliothek. Der Coach legt hier Einheiten ab, wenn du sie planst. |  |
| `renderCompetitions` | `h(.context-empty)` | Noch keine Wettkämpfe gespeichert. |  |
<!-- END GENERATED STATES -->

## Terms

One word per thing (athlete decision, #3933). `tests/copy-labels.test.cjs`
fails when the client source holds a word on the right.

| Thing | Term | Not |
| --- | --- | --- |
| Heart rate | Herzfrequenz, Herzfrequenzzonen | Puls, HF, Pulszonen, HF-Zonen |
| Cadence | Trittfrequenz (bike), Schrittfrequenz (run) | Kadenz |
| Speed | Geschwindigkeit | Tempo (Pace stays for pace sports) |
| Power zones | Leistungszonen | Power-Zonen |
| Training load (TSS, Garmin) | Belastung | Trainingsbelastung |
| Session-RPE metric | RPE-Belastung, in RPE-Punkten | Session-RPE-Last, Kurzzeitbelastung, Langzeitbelastung, AU |
| Value the athlete entered | Eingetragen | Lokal |
| Strength | Kraft | Krafttraining |
| Profile | Profil, Profil speichern | Athletenkontext |
| Hand-over to the Coach | Mit Coach besprechen | Mit Coach lösen, Mit Coach strukturieren, Mit dem Coach analysieren |
| Not set up | Nicht eingerichtet (setup), Nicht verbunden (connection), Nicht geprüft (check) | nicht konfiguriert, Nicht gesetzt, Noch nicht getestet |

## Copy deck

Every confirmation the app asks for through `requestConfirmation`, the single
confirmation dialog (also behind `confirmDiscardChanges` and
`confirmDiscardDraft`): title, message and confirm button, with the dialog's
defaults where a call gives none. Computed texts are listed as in the state
texts.

<!-- BEGIN GENERATED COPY -->
| Where | Title | Message | Button |
| --- | --- | --- | --- |
| `public/activity-details.js` `renderBundle` | Aktivität löschen? | Die Aktivität wird mit allen Aufzeichnungen lokal gelöscht. Bei den Anbietern bleibt sie erhalten. | Löschen |
| `public/analysis.js` `bindBodyFatDialog` | (computed) `` `${name} löschen?` `` | (computed) `` `${name} vom ${dateLabel(day)} wird gelöscht.` `` | Löschen |
| `public/analysis.js` `bindWeightDialog` | Gewicht löschen? | (computed) `` `Das Gewicht vom ${dateLabel(day)} wird gelöscht.` `` | Löschen |
| `public/analysis.js` `retireEquipment` | Ausrüstung ausmustern? | (computed) `` `${item.name} wird ausgemustert.` `` | Ausmustern |
| `public/auth.js` `logout` | Abmelden? | (computed) `` `Du wirst auf diesem Gerät abgemeldet. Deine Daten bleiben verschlüsselt gespeichert.${outboxLossNote()}` `` | Abmelden |
| `public/coach.js` `resetCoachChat` | Coach-Chat zurücksetzen? | Der Verlauf dieses Chats wird gelöscht, eine neue Unterhaltung beginnt. Profil, Pläne und Einträge bleiben. | Zurücksetzen |
| `public/coach.js` `undoCoachTurn` | Änderungen zurücknehmen? | Alle lokalen Änderungen dieser Coach-Antwort werden zurückgenommen. Intervals.icu bleibt unverändert. | Zurücknehmen |
| `public/diagnostics.js` `deleteServerLogs` | Server-Logs löschen? | Alle gespeicherten Server-Logs werden gelöscht. | Löschen |
| `public/diagnostics.js` `deleteDiagnostics` | Diagnose löschen? | Alle gespeicherten Diagnosedaten werden gelöscht. | Löschen |
| `public/nutrition-meal-edit.js` `saveNutritionMealEdit` | Mahlzeit löschen? | (computed) `` `Die Mahlzeit „${item.description}“ wird gelöscht, weil keine Zutat übrig ist.` `` | Löschen |
| `public/nutrition.js` `deleteNutritionProduct` | Datenbankeintrag entfernen? | (computed) `` `Der Eintrag „${product.name}“ wird aus der Datenbank entfernt. Eigene Produkte bleiben erhalten.` `` | Entfernen |
| `public/plan-views.js` `adoptStepTestValue` | Gemessenen Wert ersetzen? | Der gemessene Wert wird durch den Wert aus dem Stufentest ersetzt. | Ersetzen |
| `public/provider-connections.js` `changeMetricExportField` | Intervals.icu-Abgleich ändern? | (computed) ` message ` | Ändern |
| `public/provider-connections.js` `configureAutomaticMetricExport` | (computed) ` enabled ? "Automatischen Abgleich aktivieren?" : "Automatischen Abgleich deaktivieren?" ` | (computed) ` message ` | (computed) ` enabled ? "Aktivieren" : "Deaktivieren" ` |
| `public/provider-connections.js` `clearProviderCredential` | (computed) `` `${label} entfernen?` `` | Danach gilt wieder der Wert aus der Server-Einstellung, falls dort einer gesetzt ist. | Entfernen |
| `public/provider-connections.js` `disconnectGarmin` | Garmin trennen? | Die in der App gespeicherten Garmin-Tokens werden gelöscht. Bereits geladene Daten bleiben erhalten. | Trennen |
| `public/settings.js` `adoptFirstThreshold` | Gemessenen Wert ersetzen? | Der gemessene Wert wird durch den Wert aus DFA-α1 ersetzt. | Ersetzen |
| `public/settings.js` `restoreDatabaseBackup` | Datenbank-Backup wiederherstellen? | Das aktuelle Datenbank-Backup wird vorher gesichert und durch die ausgewählte Datei ersetzt. | Wiederherstellen |
| `public/settings.js` `deleteGpsTracks` | GPS-Spuren löschen? | (computed) `` `Alle gespeicherten GPS-Spuren (${gpsTracksState.stored_tracks}) werden unwiderruflich lokal gelöscht. Streckenerkennung und Wetter am Ort nutzen sie nicht mehr. Die übrigen Aufzeichnungen bleiben.` `` | Löschen |
| `public/settings.js` `deletePrivacyData` | Lokale Daten endgültig löschen? | (computed) ` scope ` | Endgültig löschen |
| `public/settings.js` `confirmLt2Step` | Große Abweichung | (computed) `` `${error.message} Zonen in % der Schwelle verschieben sich mit.` `` | Trotzdem übernehmen |
| `public/settings.js` `confirmLt2Step` | Eigenen Wert ersetzen? | (computed) ` error.message ` | Ersetzen |
| `public/settings.js` `removeZones` | Lokale Zonen entfernen? | Danach gelten wieder die Zonen des Anbieters. | Entfernen |
| `public/shared.js` `confirmDiscardDraft` | Änderungen verwerfen? | Deine Eingaben auf dieser Seite gehen verloren. | Verwerfen |
| `public/sync-actions.js` `fullResync` | (computed) ` confirmation.title ` | (computed) ` confirmation.message ` | Neu laden |
| `public/wahoo-connection.js` `disconnectWahoo` | (computed) `` `${label} trennen?` `` | (computed) `` `Der Zugriff wird bei ${label} widerrufen und die lokalen Zugangsdaten werden gelöscht. Bereits geladene Einheiten bleiben erhalten.` `` | Trennen |
<!-- END GENERATED COPY -->
