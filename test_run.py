"""Run the release coordination graph for a simulated order-service PR.

The dependency radius and runtime evidence come from the newest complete
baseline snapshot under ``baselines/default/default/`` (no live telemetry, no
mock_telemetry.json). Risk
reasoning calls Claude, so Anthropic credentials must be available, e.g.
``export ANTHROPIC_API_KEY=...``.
"""

from src.coordinator.graph import app
from src.coordinator.state import ReleaseGraphState

MOCK_PULL_REQUEST: dict[str, object] = {
    "pr_id": "PR-2048",
    "title": "Require customerId on POST /order and persist order priority",
    # FQURI; unknown environment/cluster segments match any registered value.
    "target_service": "urn:release-coord:default:unknown:unknown:default:order-service",
    # customerId becomes required: consumers must deploy before the target.
    "change_type": "breaking",
    "source_branch": "feature/order-priority",
    "target_branch": "main",
    "description": (
        "Adds a mandatory customerId to new orders and a priority field that "
        "is published on the order queue for makeline-service to consume."
    ),
    "diff": """\
diff --git a/src/order-service/routes/root.js b/src/order-service/routes/root.js
index 3f1c2aa..9b7e410 100644
--- a/src/order-service/routes/root.js
+++ b/src/order-service/routes/root.js
@@ -4,12 +4,19 @@ module.exports = async function (fastify, opts) {
   fastify.post('/', async function (request, reply) {
-    const msg = request.body
+    const { customerId, items, priority = 'standard' } = request.body
+    if (!customerId) {
+      reply.code(400)
+      return { error: 'customerId is required' }
+    }
+    const msg = { customerId, items, priority, schemaVersion: 2 }
     fastify.sendMessage(Buffer.from(JSON.stringify(msg)))
     reply.code(201)
     return msg
   })
diff --git a/src/order-service/schema/order.json b/src/order-service/schema/order.json
index 1a2b3c4..5d6e7f8 100644
--- a/src/order-service/schema/order.json
+++ b/src/order-service/schema/order.json
@@ -1,8 +1,10 @@
 {
   "type": "object",
-  "required": ["items"],
+  "required": ["customerId", "items"],
   "properties": {
-    "customerId": { "type": "string" },
+    "customerId": { "type": "string", "minLength": 1 },
+    "priority": { "enum": ["standard", "express"] },
+    "schemaVersion": { "const": 2 },
     "items": { "type": "array" }
   }
 }
""",
}


if __name__ == "__main__":
    initial_state: ReleaseGraphState = {
        "trigger_event": MOCK_PULL_REQUEST,
        # Blank: read runtime evidence from the stored baseline only.
        "workspace_id": "",
    }
    final_state = app.invoke(initial_state)
    print(final_state["evidence_report"])
