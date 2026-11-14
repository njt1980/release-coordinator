"""Advisory change flags: migrations, configuration keys and feature flags (plan item 4e)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from fastapi import BackgroundTasks

from src import webhooks
from src.coordinator import nodes as coordinator_nodes
from src.coordinator.change_flags import (
    config_format,
    evaluate_change_flags,
    is_feature_flag_key,
    migration_stack,
    parse_unified_diff,
)
from src.coordinator.policy import decide_release
from src.coordinator.sequencing import DeploymentPlan
from src.core.config_keys import ConfigFormat, ConfigParseError, diff_keys, read_keys, strip_json_comments
from src.core.models import (
    AssessmentEvidence,
    ChangeFlag,
    ChangeFlagKind,
    ChangeFlags,
    ContractChange,
    ReasonCode,
    ReleaseAssessment,
    RiskAssessment,
)
from src.orchestration.briefing import build_briefing
from tests.fake_devops import FakeDevOps
from tests.test_pr_webhook import BASE, HEAD, BaselineFixture, _payload

APPSETTINGS_BASE = """{
  // connection strings
  "ConnectionStrings": { "Orders": "Server=db;Password=secret" },
  "FeatureManagement": { "NewCheckout": false },
  "Logging": { "LogLevel": { "Default": "Information" } },
}"""
APPSETTINGS_HEAD = """{
  "ConnectionStrings": { "Orders": "Server=db2;Password=other" },
  "FeatureManagement": { "NewCheckout": true, "FastShipping": false },
  "Payments": { "Url": "http://payments" }
}"""


class ParserTests(unittest.TestCase):
    def test_json_comments_trailing_commas_and_nesting(self) -> None:
        keys = read_keys(APPSETTINGS_BASE, ConfigFormat.JSON)
        self.assertEqual(
            sorted(keys), ["ConnectionStrings:Orders", "FeatureManagement:NewCheckout", "Logging:LogLevel:Default"]
        )
        # Values are digests only, never the value itself.
        self.assertNotIn("secret", "".join(keys.values()))
        self.assertEqual(strip_json_comments('{"u": "http://x//y", /* c */ "a": [1,],}'), '{"u": "http://x//y",  "a": [1]}')

    def test_yaml_profiles_properties_and_dotenv(self) -> None:
        yaml_keys = read_keys("spring:\n  datasource:\n    url: a\n---\nserver:\n  port: 1\n", ConfigFormat.YAML)
        self.assertEqual(sorted(yaml_keys), ["#1/server.port", "spring.datasource.url"])
        props = read_keys("# c\nserver.port=80\nfeatures.beta: on\nlong.key = a \\\n  b\n", ConfigFormat.PROPERTIES)
        self.assertEqual(sorted(props), ["features.beta", "long.key", "server.port"])
        env = read_keys("export API_URL='x'\n# c\nFEATURE_BETA=1\n", ConfigFormat.DOTENV)
        self.assertEqual(sorted(env), ["API_URL", "FEATURE_BETA"])

    def test_malformed_json_is_a_parse_error(self) -> None:
        with self.assertRaises(ConfigParseError):
            read_keys("{ nope", ConfigFormat.JSON)

    def test_diff_keys(self) -> None:
        delta = diff_keys({"a": "1", "b": "2"}, {"b": "3", "c": "4"})
        self.assertEqual((delta.added, delta.removed, delta.changed), (["c"], ["a"], ["b"]))


class ClassificationTests(unittest.TestCase):
    def test_migrations(self) -> None:
        self.assertEqual(migration_stack("src/Ordering.Infrastructure/Migrations/20240101_Init.cs"), "dotnet")
        self.assertIsNone(migration_stack("src/Ordering.Infrastructure/Migrations/20240101_Init.Designer.cs"))
        self.assertEqual(migration_stack("svc/src/main/resources/db/migration/V2__add_col.sql"), "java")
        self.assertEqual(migration_stack("svc/src/main/resources/db/changelog/db.changelog-master.yaml"), "java")
        self.assertIsNone(migration_stack("svc/src/main/resources/db/migration/README.md"))
        self.assertIsNone(migration_stack("src/Orders/Controllers/MigrationsController.cs"))

    def test_config_files(self) -> None:
        self.assertEqual(config_format("src/Api/appsettings.Production.json"), (ConfigFormat.JSON, "dotnet"))
        self.assertEqual(config_format("svc/application-prod.yml"), (ConfigFormat.YAML, "java"))
        self.assertEqual(config_format("svc/bootstrap.properties"), (ConfigFormat.PROPERTIES, "java"))
        self.assertEqual(config_format("web/.env.production"), (ConfigFormat.DOTENV, "node"))
        self.assertIsNone(config_format("web/package.json"))

    def test_feature_flag_sections(self) -> None:
        self.assertTrue(is_feature_flag_key("FeatureManagement:Beta", ConfigFormat.JSON))
        self.assertFalse(is_feature_flag_key("Features:Beta", ConfigFormat.JSON))
        self.assertTrue(is_feature_flag_key("togglz.features.BETA.enabled", ConfigFormat.YAML))
        self.assertTrue(is_feature_flag_key("#1/features.beta", ConfigFormat.YAML))
        self.assertTrue(is_feature_flag_key("FF_BETA", ConfigFormat.DOTENV))
        self.assertFalse(is_feature_flag_key("API_URL", ConfigFormat.DOTENV))


DIFF = """diff --git a/web/.env b/web/.env
--- a/web/.env
+++ b/web/.env
@@ -1,2 +1,3 @@
-API_URL=http://old
+API_URL=http://new
+FEATURE_BETA=1
diff --git a/db/migration/V3__x.sql b/db/migration/V3__x.sql
new file mode 100644
--- /dev/null
+++ b/db/migration/V3__x.sql
@@ -0,0 +1 @@
+ALTER TABLE t ADD c int;
"""


class EvaluateTests(unittest.TestCase):
    def test_reads_base_and_head_when_source_control_is_there(self) -> None:
        path = "src/Api/appsettings.json"
        files = {(path, BASE): APPSETTINGS_BASE, (path, HEAD): APPSETTINGS_HEAD}
        flags = evaluate_change_flags([path], "x", lambda p, c: files.get((p, c)), BASE, HEAD)
        by_kind = {flag.kind: flag for flag in flags.flags}
        config = by_kind[ChangeFlagKind.CONFIG]
        self.assertEqual(config.added_keys, ["Payments:Url"])
        self.assertEqual(config.removed_keys, ["Logging:LogLevel:Default"])
        self.assertEqual(config.changed_keys, ["ConnectionStrings:Orders"])
        self.assertTrue(config.exact)
        flag = by_kind[ChangeFlagKind.FEATURE_FLAG]
        self.assertEqual((flag.added_keys, flag.changed_keys), (["FeatureManagement:FastShipping"], ["FeatureManagement:NewCheckout"]))
        self.assertNotIn("other", config.detail)
        self.assertEqual(
            flags.reason_codes, [ReasonCode.CONFIG_KEYS_CHANGED, ReasonCode.FEATURE_FLAGS_CHANGED]
        )

    def test_an_unchanged_key_set_flags_nothing(self) -> None:
        path = "svc/application.yml"
        text = "server:\n  port: 80\n"
        flags = evaluate_change_flags([path], "x", lambda p, c: text, BASE, HEAD)
        self.assertEqual(flags.flags, [])

    def test_falls_back_to_the_diff_without_source_control(self) -> None:
        flags = evaluate_change_flags(["web/.env", "db/migration/V3__x.sql", "web/app.js"], DIFF, None, "", "")
        kinds = {(flag.kind, flag.path): flag for flag in flags.flags}
        config = kinds[(ChangeFlagKind.CONFIG, "web/.env")]
        self.assertEqual(config.changed_keys, ["API_URL"])
        self.assertFalse(config.exact)
        self.assertEqual(kinds[(ChangeFlagKind.FEATURE_FLAG, "web/.env")].added_keys, ["FEATURE_BETA"])
        migration = kinds[(ChangeFlagKind.MIGRATION, "db/migration/V3__x.sql")]
        self.assertTrue(migration.file_added)
        self.assertEqual(len(flags.flags), 3)

    def test_unreadable_file_is_recorded_and_falls_back(self) -> None:
        def fail(path: str, commit: str) -> str | None:
            raise RuntimeError("boom")

        flags = evaluate_change_flags(["web/.env"], DIFF, fail, BASE, HEAD)
        self.assertIn("boom", flags.unreadable["web/.env"])
        self.assertTrue(all(not flag.exact for flag in flags.flags))

    def test_parse_unified_diff(self) -> None:
        sections = parse_unified_diff(DIFF)
        self.assertEqual(sorted(sections), ["db/migration/V3__x.sql", "web/.env"])
        self.assertEqual(sections["web/.env"].minus, ["API_URL=http://old"])


class VerdictTests(unittest.TestCase):
    def test_flags_are_advisory_and_block_automatic_approval(self) -> None:
        plan = DeploymentPlan(
            change_type=ContractChange.ADDITIVE,
            sequence=["a"],
            services_to_deploy=["a"],
            compatibility_verified=True,
        )
        advisory = RiskAssessment(risk_score=0.1, approved=True, justification="ok")
        verdict = decide_release([], plan, advisory, change_codes=[ReasonCode.DB_MIGRATION])
        self.assertEqual(verdict.reason_codes, [ReasonCode.DB_MIGRATION])
        self.assertFalse(verdict.approved)
        self.assertTrue(verdict.actionable)
        for code in (ReasonCode.DB_MIGRATION, ReasonCode.CONFIG_KEYS_CHANGED, ReasonCode.FEATURE_FLAGS_CHANGED):
            self.assertFalse(code.limits_actionability)


class EndToEndTests(BaselineFixture):
    """The coordinator graph on a pull request changing order-service's config."""

    CONFIG = "src/order-service/appsettings.json"

    def _run(self) -> Any:
        self._write_baseline()
        payload = _payload(changed_files=[self.CONFIG], change_type="additive")
        tasks = BackgroundTasks()
        webhooks.pr_assessment_webhook(payload, tasks)
        devops = FakeDevOps(
            history=[BASE, HEAD],
            changes={(BASE, HEAD): [self.CONFIG]},
            files={(self.CONFIG, BASE): APPSETTINGS_BASE, (self.CONFIG, HEAD): APPSETTINGS_HEAD},
        )
        with (
            patch.object(coordinator_nodes, "BASELINE_STORE", self.store),
            patch.object(coordinator_nodes, "RiskReasoner") as reasoner_cls,
            patch.object(coordinator_nodes, "DevOpsExtractor", devops),
        ):
            reasoner_cls.return_value.assess.return_value = RiskAssessment(
                risk_score=0.1, approved=True, justification="low risk"
            )
            final = webhooks.coordinator_app.invoke(tasks.tasks[0].args[2])
        return final["release_assessment"]

    def test_config_change_reaches_the_verdict_and_briefing(self) -> None:
        assessment = self._run()
        self.assertIn(ReasonCode.CONFIG_KEYS_CHANGED, assessment.reason_codes)
        self.assertIn(ReasonCode.FEATURE_FLAGS_CHANGED, assessment.reason_codes)
        flags = assessment.evidence.change_flags
        self.assertIsNotNone(flags)
        self.assertTrue(all(flag.exact for flag in flags.flags))
        keys = [group.root_key for group in build_briefing(assessment).groups]
        self.assertIn("decision:change-config", keys)
        self.assertIn("decision:change-feature-flag", keys)


class HeadlineTests(unittest.TestCase):
    def test_complete_evidence_with_only_flags_is_your_call(self) -> None:
        flag = ChangeFlag(kind=ChangeFlagKind.MIGRATION, path="m/V1__a.sql", stack="java", detail="m/V1__a.sql: new database migration")
        assessment = ReleaseAssessment(
            approved=False,
            actionable=True,
            reason_codes=[ReasonCode.DB_MIGRATION],
            deployment_sequence=["a"],
            justification="j",
            evidence=AssessmentEvidence(pr_id="1", change_flags=ChangeFlags(flags=[flag])),
        )
        briefing = build_briefing(assessment)
        self.assertTrue(briefing.headline.startswith("Your call"))
        group = next(g for g in briefing.groups if g.root_key == "decision:change-migration")
        self.assertIn("m/V1__a.sql: new database migration", group.what_to_check)


if __name__ == "__main__":
    unittest.main()
