# Frigate CI fixtures

`model_cache/` is a stand-in for the Frigate+ model cache, used only by
`.github/workflows/frigate-config.yaml`.

`config.yml` sets `model.path: plus://<model id>`. When Frigate loads that, it
checks `/config/model_cache/<model id>` and `<model id>.json`, and calls the
Frigate+ API to download both if either is missing — which would mean a
`PLUS_API_KEY` secret and network access in CI. Seeding the cache with a stub
skips the download, so the check stays offline and keyless.

`<model id>` is an empty file (Frigate only hashes it). `<model id>.json` is the
model metadata Frigate reads: dimensions, tensor layout, the detector types the
model supports, and the label map.

Two things to know about it:

- **It is keyed by model id.** Point `config.yml` at a different Frigate+ model
  and the workflow fails with a message telling you to add a matching pair of
  files here.
- **The label map is invented.** Frigate only *warns* when a camera tracks a
  label the model does not list, so a wrong label map cannot fail the check;
  the labels here are the ones `config.yml` tracks, which keeps the CI log
  clean. Everything else in the file is real and is validated by Frigate.

The alternative is to drop this directory, add a `PLUS_API_KEY` repository
secret, pass it into the container, and give the job network access. That
validates the model reference for real, at the cost of a secret and a
dependency on the Frigate+ API being up.
