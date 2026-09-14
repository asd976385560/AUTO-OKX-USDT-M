import unittest
from pathlib import Path
from unittest import mock
from scripts import audit_candidate_bundle_rollout as audit

CYCLE='2026-09-13T07:45'
PATHS={'manifest':Path('manifest.json'),'bundle':Path('bundle.json')}
def manifest():
    value={'cycle_id':CYCLE,'schema':'fixture'}
    return {**value,'manifest_sha256':audit._canonical_sha256(value)}

class AuditManifestReuseTests(unittest.TestCase):
    def test_success_still_runs_full_bundle_validator_and_binds_content(self):
        m=manifest();b={'candidate_manifest_sha256':m['manifest_sha256']}
        with mock.patch.object(audit,'_load_json',return_value=m),mock.patch.object(audit,'load_candidate_manifest') as direct,mock.patch.object(audit,'load_candidate_evidence_bundle',return_value=b) as full:
            self.assertEqual((m,b,None),audit._load_audit_artifact_pair(PATHS,CYCLE))
            direct.assert_not_called();full.assert_called_once_with(PATHS['bundle'],expected_cycle=CYCLE,expected_manifest_path=PATHS['manifest'])
    def test_changed_manifest_cannot_use_validated_different_hash(self):
        m=manifest()
        with mock.patch.object(audit,'_load_json',return_value=m),mock.patch.object(audit,'load_candidate_manifest',side_effect=ValueError('manifest changed')) as direct,mock.patch.object(audit,'load_candidate_evidence_bundle',return_value={'candidate_manifest_sha256':'f'*64}):
            self.assertEqual((None,None,'ValueError:manifest changed'),audit._load_audit_artifact_pair(PATHS,CYCLE));direct.assert_called_once()
    def test_bad_bundle_keeps_original_valid_manifest_diagnostic(self):
        m=manifest()
        with mock.patch.object(audit,'_load_json',return_value=m),mock.patch.object(audit,'load_candidate_manifest',return_value=(m,'bytes-hash')),mock.patch.object(audit,'load_candidate_evidence_bundle',side_effect=ValueError('bundle invalid')):
            self.assertEqual((m,None,'ValueError:bundle invalid'),audit._load_audit_artifact_pair(PATHS,CYCLE))
    def test_bad_manifest_retains_original_first_error(self):
        m=manifest();m['cycle_id']='wrong'
        with mock.patch.object(audit,'_load_json',return_value=m),mock.patch.object(audit,'load_candidate_manifest',side_effect=ValueError('candidate manifest cycle mismatch')),mock.patch.object(audit,'load_candidate_evidence_bundle') as full:
            self.assertEqual((None,None,'ValueError:candidate manifest cycle mismatch'),audit._load_audit_artifact_pair(PATHS,CYCLE));full.assert_not_called()
