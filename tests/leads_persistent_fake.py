"""A FakeFR that actually stores what it creates and updates.

The shared `FakeFR` in conftest.py only echoes each write back (`_last`),
which is enough for the main suite's "did we send the right params" tests,
but this package's dedupe/idempotency depends on a create being *visible* to
a later search in the same test -- re-running the pipeline and asserting
"zero new customers" only means something if a customer really was created
the first time. Kept out of conftest.py so the 233 tests that already pass
against the shared fixture are untouched by this.
"""

from __future__ import annotations

import os
from typing import Any

from conftest import FakeFR

# Field name FieldRoutes uses for a freshly created row's own ID, by entity.
# "task" specifically is unverified live (this repo's own notes: GET returns
# tasks under "taskIDs", not "taskID", and a note's *write* param for its own
# row is "contactID", not "noteID" -- so guessing wrong here is plausible).
# The production code (`fr_push._extract_id`) is deliberately defensive about
# this; this fake just needs to be internally consistent with itself.
_CREATE_ID_FIELD = {"customer": "customerID", "note": "noteID", "task": "taskIDs", "subscription": "subscriptionID"}


class PersistentFakeFR(FakeFR):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._next_id: dict[str, int] = {}

    def _write(self, entity: str, action: str, form: dict[str, list[str]]) -> dict[str, Any]:
        if self.fail_write_after is not None and self.writes_today == self.fail_write_after:
            self.fail_write_after = None
            return {"success": False, "errorMessage": self.fail_write_message}
        if entity == "spot" and action == "reserve":
            return {"success": True, "reservation": "tok-123"}
        if action == "create":
            new_id = self._next_id.get(entity, 5000)
            self._next_id[entity] = new_id + 1
            record = {
                k.rstrip("[]"): v[0] for k, v in form.items() if k not in ("authenticationKey", "authenticationToken")
            }
            id_field = _CREATE_ID_FIELD.get(entity, f"{entity}ID")
            record[id_field] = new_id
            # A real single-office tenant assigns a new record to the API key's office even
            # though most create endpoints (customer/create included) take no officeID param
            # -- server._with_office() scopes every subsequent search by FR_OFFICE_ID, so a
            # record with no officeID at all would wrongly vanish from a dedupe search here.
            office = os.environ.get("FR_OFFICE_ID")
            if office and "officeID" not in record:
                record["officeID"] = office
            self.data.setdefault(entity, {})[new_id] = record
            return {"success": True, id_field: new_id}
        if action == "update":
            record_id = None
            for key in (f"{entity}ID", "contactID", "customerID"):
                if key in form:
                    record_id = int(form[key][0])
                    break
            if record_id is not None and record_id in self.data.get(entity, {}):
                for k, v in form.items():
                    if k in ("authenticationKey", "authenticationToken"):
                        continue
                    self.data[entity][record_id][k.rstrip("[]")] = v[0]
            return {"success": True}
        return {"success": True}
