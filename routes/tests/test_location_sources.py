import io
import gzip
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase

from routes.services.location_sources import (
    OSM_TILES, SourceError, download_osm_snapshot, download_source, read_json, source_lock,
)


class SourceDownloadTests(SimpleTestCase):
    def test_regional_download_resumes_and_deduplicates_boundary_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            fetched = []
            def fetch(directory, kind, endpoint, query, filename, allow_empty=False):
                path = directory / filename
                if path.exists():
                    return path
                if filename == "osm-tile-1.json" and len(fetched) == 1:
                    fetched.append("failed")
                    raise SourceError("provider unavailable")
                fetched.append(filename)
                path.write_text(json.dumps({"elements": [{"type": "node", "id": 1}]}))
                return path
            with patch("routes.services.location_sources.download_source", side_effect=fetch):
                with self.assertRaises(SourceError):
                    download_osm_snapshot(directory)
                self.assertTrue((directory / "osm-tile-0.json").exists())
                self.assertFalse((directory / "osm-fuel.json").exists())
                path = download_osm_snapshot(directory)
                document = read_json(path)[0]
                self.assertEqual(len(document["source_parts"]), len(OSM_TILES))
                self.assertEqual(document["elements"], [{"type": "node", "id": 1}])
                self.assertEqual(fetched.count("osm-tile-0.json"), 1)
            with patch("routes.services.location_sources.download_source", side_effect=AssertionError("cached")):
                self.assertEqual(download_osm_snapshot(directory), path)

    def test_compressed_artifact_has_same_content_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.json"
            zipped = Path(folder) / "source.json.gz"
            path.write_bytes(b'{"elements": []}')
            zipped.write_bytes(gzip.compress(path.read_bytes(), mtime=0))
            self.assertEqual(read_json(path), read_json(zipped))

    def test_cached_download_never_refetches_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with patch("routes.services.location_sources.urlopen", return_value=io.BytesIO(b'{"elements":[{"id":1}]}')) as fetch:
                path = download_source(directory, "osm")
                self.assertEqual(download_source(directory, "osm"), path)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(read_json(directory / "osm-manifest.json")[0]["sha256"], read_json(path)[1])

    def test_failure_cooldown_survives_new_invocation_then_allows_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with patch("routes.services.location_sources.time.time", return_value=100), \
                 patch("routes.services.location_sources.urlopen", side_effect=HTTPError("url", 429, "rate limit", {"Retry-After": "120"}, None)):
                with self.assertRaisesMessage(SourceError, "retry after 120"):
                    download_source(directory, "osm")
            with patch("routes.services.location_sources.time.time", return_value=150), \
                 patch("routes.services.location_sources.urlopen") as fetch:
                with self.assertRaisesMessage(SourceError, "cooldown"):
                    download_source(directory, "osm")
                fetch.assert_not_called()
            with patch("routes.services.location_sources.time.time", return_value=221), \
                 patch("routes.services.location_sources.urlopen", return_value=io.BytesIO(b'{"elements":[{"id":1}]}')):
                self.assertTrue(download_source(directory, "osm").exists())

    def test_partial_malformed_and_network_failure_never_become_cache(self):
        for payload in (b'{"elements":', b'{"elements":[{}],"remark":"runtime timeout"}', b'{"error":{}}'):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder)
                with patch("routes.services.location_sources.urlopen", return_value=io.BytesIO(payload)):
                    with self.assertRaises(SourceError):
                        download_source(directory, "osm")
                self.assertFalse((directory / "osm-fuel.json").exists())
                self.assertFalse((directory / "download.lock").exists())
        with tempfile.TemporaryDirectory() as folder:
            with patch("routes.services.location_sources.urlopen", side_effect=URLError("offline")):
                with self.assertRaises(SourceError):
                    download_source(Path(folder), "osm")

    def test_retry_after_http_date_is_persisted(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            error = HTTPError("url", 503, "busy", {"Retry-After": "Thu, 01 Jan 1970 02:00:00 GMT"}, None)
            with patch("routes.services.location_sources.time.time", return_value=100), \
                 patch("routes.services.location_sources.urlopen", side_effect=error):
                with self.assertRaises(SourceError):
                    download_source(directory, "osm")
            self.assertGreaterEqual(read_json(directory / "osm-request.json")[0]["next_request_at"], 7200)

    def test_download_lock_prevents_parallel_processes(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            with source_lock(directory):
                with self.assertRaisesMessage(SourceError, "Another source download"):
                    download_source(directory, "osm")
