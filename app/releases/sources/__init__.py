"""Where an assessment's evidence comes from. Each source returns
`app.releases.evidence.EvidenceItem`s and raises `SourceError` on failure;
`app.releases.evidence.gather` keeps going when one source fails."""
