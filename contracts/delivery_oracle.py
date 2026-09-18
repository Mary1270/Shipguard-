# { "Depends": "py-genlayer:1jb45aa8ynh2a9c9xn3b7qqh8sm5q93hwfp7jqmwsfhh8jpz09h6" }
from genlayer import *
import json
import datetime


class DeliveryOracle(gl.Contract):
    """
    DeliveryOracle - ShipGuard parametric shipping-delay insurance.

    Canonical event identity is a policy's shipment (carrier + tracking
    sources), read from the linked PolicyRegistry. Evidence is locked
    only after `evidence_lock_time` (declared_eta + evidence_buffer),
    requires at least two independent tracking sources to agree before
    confirming DELAYED or ON_TIME, and resolves in two phases: an
    optimistic decision followed by a 48-hour, evidence-based challenge
    window. Each side (insured/underwriter) may challenge at most once
    per policy, bounding resolution to a small, predictable number of
    rounds. Resolution history is versioned and immutable - earlier
    versions are appended to `history`, never overwritten.

    See policy_registry.py's module docstring for the conventions this
    deliberately carries forward from the sibling MatchGuard project
    (gl.vm.UserError, JSON-in-TreeMap storage, str addresses,
    real-clock `_now_utc()`), and for which parts of this file - value
    handling and cross-contract calls - are new and unproven, since
    MatchGuard never needed either.

    Per GenLayer's own guidance, LLM-derived output is compared with
    `gl.eq_principle.prompt_comparative` here, never `strict_eq`
    (which is only appropriate for byte-identical deterministic
    values).
    """

    DECISION_DELAYED = "DELAYED"
    DECISION_ON_TIME = "ON_TIME"
    DECISION_INCONCLUSIVE = "INCONCLUSIVE"

    CHALLENGE_WINDOW_HOURS = 48
    MIN_AGREEING_SOURCES = 2

    owner: str
    registry_address: str
    resolutions: TreeMap[str, str]
    # immutable audit trail: "{policy_id}:{version}" -> decision at that version
    history: TreeMap[str, str]

    def __init__(self):
        self.owner = self._address_to_str(gl.message.sender_address)
        self.registry_address = ""

    # ----------------------------------------------------------------
    # Helpers (identical conventions to policy_registry.py)
    # ----------------------------------------------------------------

    def _address_to_str(self, value) -> str:
        try:
            return str(Address(str(value)))
        except Exception:
            raise gl.vm.UserError(f"{value!r} is not a valid on-chain address.")

    def _now_utc(self):
        return datetime.datetime.now(datetime.timezone.utc)

    def _parse_iso8601_utc(self, raw: str):
        if raw is None:
            return None
        text = str(raw).strip()
        if not text:
            return None
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.astimezone(datetime.timezone.utc)

    def _load(self, policy_id: str) -> dict:
        raw = self.resolutions.get(policy_id)
        if raw is None:
            raise gl.vm.UserError(f"no resolution exists yet for policy_id {policy_id!r}.")
        return json.loads(raw)

    def _save(self, policy_id: str, resolution: dict) -> None:
        self.resolutions[policy_id] = json.dumps(resolution, sort_keys=True)
        self.history[f"{policy_id}:{resolution['version']}"] = resolution["decision"]

    def _get_policy(self, policy_id: str) -> dict:
        raw = gl.get_contract_at(Address(self.registry_address)).view().get_policy_json(
            policy_id
        )
        return json.loads(raw)

    def _evaluate(self, tracking_ref: str, declared_eta_iso: str, threshold_days: int) -> tuple:
        sources = [s.strip() for s in tracking_ref.split("|") if s.strip()]
        if len(sources) < self.MIN_AGREEING_SOURCES:
            return (self.DECISION_INCONCLUSIVE, "fewer than two tracking sources configured.")

        sources_copy = sources
        eta_copy = declared_eta_iso
        threshold_copy = int(threshold_days)

        def get_verdicts() -> str:
            verdicts = []
            for source in sources_copy:
                try:
                    page = gl.nondet.web.render(source, mode="text")
                    prompt = f"""
You are extracting a shipment delivery verdict from tracking page text.

Declared ETA (ISO-8601 UTC): {eta_copy}
Delay threshold (days): {threshold_copy}

Tracking page text:
{page}

Determine whether the shipment was delivered, and if so whether the
actual delivery time is more than {threshold_copy} days after the
declared ETA.

Respond using ONLY this JSON format, no other text:
{{"delivered": true|false, "verdict": "DELAYED"|"ON_TIME"|"UNKNOWN", "detail": "short reason"}}
"""
                    result = gl.nondet.exec_prompt(prompt)
                except Exception as exc:
                    # A source that fails to load (dead link, 404, timeout)
                    # should not crash the whole resolution - it just
                    # doesn't get a vote. Found via live Studio testing:
                    # an unreachable tracking URL previously propagated
                    # as a raw, unrecoverable contract error instead of
                    # degrading gracefully toward INCONCLUSIVE.
                    result = json.dumps(
                        {
                            "delivered": False,
                            "verdict": "UNKNOWN",
                            "detail": f"source unavailable: {exc}",
                        }
                    )
                verdicts.append(result)
            return json.dumps(verdicts, sort_keys=True)

        raw_verdicts = gl.eq_principle.prompt_comparative(
            get_verdicts,
            principle="The set of per-source verdicts (DELAYED / ON_TIME / UNKNOWN) must match",
        )

        try:
            parsed = [json.loads(v) for v in json.loads(raw_verdicts)]
        except Exception:
            return (self.DECISION_INCONCLUSIVE, "could not parse validator verdicts.")

        delayed_votes = sum(1 for p in parsed if p.get("verdict") == "DELAYED")
        on_time_votes = sum(1 for p in parsed if p.get("verdict") == "ON_TIME")

        if delayed_votes >= self.MIN_AGREEING_SOURCES and delayed_votes > on_time_votes:
            return (self.DECISION_DELAYED, f"{delayed_votes}/{len(parsed)} sources agree: delayed.")
        if on_time_votes >= self.MIN_AGREEING_SOURCES and on_time_votes > delayed_votes:
            return (self.DECISION_ON_TIME, f"{on_time_votes}/{len(parsed)} sources agree: on time.")
        return (self.DECISION_INCONCLUSIVE, "sources did not reach a two-source agreement.")

    # ----------------------------------------------------------------
    # Owner-only, one-time wiring
    # ----------------------------------------------------------------

    @gl.public.write
    def set_registry(self, registry_address: str) -> str:
        caller = self._address_to_str(gl.message.sender_address)
        if caller.lower() != self.owner.lower():
            raise gl.vm.UserError("only the deploying owner may call set_registry.")
        if self.registry_address:
            raise gl.vm.UserError("registry has already been set.")
        self.registry_address = self._address_to_str(registry_address)
        return "ok"

    # ----------------------------------------------------------------
    # Public write methods
    # ----------------------------------------------------------------

    @gl.public.write
    def request_resolution(self, policy_id: str) -> str:
        """
        Runs the nondet evidence evaluation and stores the resolution.

        Deliberately does NOT call the registry here: a proven sibling
        pattern (ProofWorksEscrow's evaluate_task / finalize_task split)
        keeps a method's nondet block and any cross-contract call in
        separate transactions rather than mixing them in one. The
        registry's `mark_resolving` (which also forwards the escrowed
        GEN to the vault) is deferred to `finalize_resolution`, which
        never runs a nondet block itself - except on the one path below
        where evaluation is skipped entirely (final_deadline already
        passed), where it's safe to do both in this same call.
        """
        if policy_id in self.resolutions:
            raise gl.vm.UserError("resolution already requested for this policy.")

        policy = self._get_policy(policy_id)
        if policy["state"] != "ACTIVE":
            raise gl.vm.UserError("policy is not active.")

        now = self._now_utc()
        evidence_lock_dt = self._parse_iso8601_utc(policy["evidence_lock_time"])
        if now < evidence_lock_dt:
            raise gl.vm.UserError("evidence lock time has not been reached yet.")

        final_deadline_dt = self._parse_iso8601_utc(policy["final_deadline"])
        past_deadline = now >= final_deadline_dt

        if past_deadline:
            # No nondet call on this path - safe to also transition the
            # registry in this same transaction.
            decision, summary = (
                self.DECISION_INCONCLUSIVE,
                "final deadline had already passed before resolution was ever requested.",
            )
        else:
            decision, summary = self._evaluate(
                policy["tracking_ref"], policy["declared_eta"], policy["delay_threshold_days"]
            )

        challenge_deadline_dt = now + datetime.timedelta(hours=self.CHALLENGE_WINDOW_HOURS)
        self._save(
            policy_id,
            {
                "policy_id": policy_id,
                "decision": decision,
                "evidence_summary": summary,
                "version": 1,
                "locked_at": now.isoformat(),
                "challenge_deadline": challenge_deadline_dt.isoformat(),
                "challenged_by_insured": False,
                "challenged_by_underwriter": False,
                "is_final": bool(past_deadline),
            },
        )

        if past_deadline:
            gl.get_contract_at(Address(self.registry_address)).emit(
                on="accepted"
            ).mark_resolving(policy_id)

        return "ok"

    @gl.public.write
    def challenge_resolution(self, policy_id: str, extra_evidence_url: str) -> str:
        resolution = self._load(policy_id)
        if resolution["is_final"]:
            raise gl.vm.UserError("resolution is already final.")

        policy = self._get_policy(policy_id)
        caller = self._address_to_str(gl.message.sender_address)
        insured = policy["insured"]
        underwriter = policy["underwriter"]
        if caller.lower() not in (insured.lower(), underwriter.lower()):
            raise gl.vm.UserError("only the insured or underwriter may challenge a resolution.")

        now = self._now_utc()
        final_deadline_dt = self._parse_iso8601_utc(policy["final_deadline"])
        if now >= final_deadline_dt:
            resolution["decision"] = self.DECISION_INCONCLUSIVE
            resolution["is_final"] = True
            self._save(policy_id, resolution)
            # No nondet call on this path (evaluation is skipped
            # entirely below) - safe to also transition the registry
            # here, same reasoning as request_resolution's past_deadline
            # branch.
            gl.get_contract_at(Address(self.registry_address)).emit(
                on="accepted"
            ).mark_resolving(policy_id)
            return "ok"

        challenge_deadline_dt = self._parse_iso8601_utc(resolution["challenge_deadline"])
        if now >= challenge_deadline_dt:
            raise gl.vm.UserError("challenge window has closed.")

        if caller.lower() == insured.lower() and resolution["challenged_by_insured"]:
            raise gl.vm.UserError("insured has already used their one challenge for this policy.")
        if caller.lower() == underwriter.lower() and resolution["challenged_by_underwriter"]:
            raise gl.vm.UserError("underwriter has already used their one challenge for this policy.")

        combined_ref = policy["tracking_ref"] + "|" + str(extra_evidence_url).strip()
        decision, summary = self._evaluate(
            combined_ref, policy["declared_eta"], policy["delay_threshold_days"]
        )

        if caller.lower() == insured.lower():
            resolution["challenged_by_insured"] = True
        else:
            resolution["challenged_by_underwriter"] = True
        resolution["decision"] = decision
        resolution["evidence_summary"] = summary
        resolution["version"] = int(resolution["version"]) + 1
        resolution["challenge_deadline"] = (
            now + datetime.timedelta(hours=self.CHALLENGE_WINDOW_HOURS)
        ).isoformat()
        self._save(policy_id, resolution)
        return "ok"

    @gl.public.write
    def finalize_resolution(self, policy_id: str) -> str:
        """
        Never runs a nondet block itself, so it's always safe to follow
        up with the registry cross-contract call here: this is where
        `mark_resolving` (state transition + escrow forwarding) actually
        happens for the normal path, deferred from `request_resolution`
        for the reason documented there.
        """
        resolution = self._load(policy_id)
        if resolution["is_final"]:
            return "ok"

        policy = self._get_policy(policy_id)
        now = self._now_utc()
        final_deadline_dt = self._parse_iso8601_utc(policy["final_deadline"])
        if now >= final_deadline_dt:
            resolution["decision"] = self.DECISION_INCONCLUSIVE
            resolution["is_final"] = True
            self._save(policy_id, resolution)
            gl.get_contract_at(Address(self.registry_address)).emit(
                on="accepted"
            ).mark_resolving(policy_id)
            return "ok"

        challenge_deadline_dt = self._parse_iso8601_utc(resolution["challenge_deadline"])
        if now < challenge_deadline_dt:
            raise gl.vm.UserError("challenge window is still open.")

        resolution["is_final"] = True
        self._save(policy_id, resolution)
        gl.get_contract_at(Address(self.registry_address)).emit(on="accepted").mark_resolving(
            policy_id
        )
        return "ok"

    # ----------------------------------------------------------------
    # Public view methods
    # ----------------------------------------------------------------

    @gl.public.view
    def get_resolution_json(self, policy_id: str) -> str:
        return self.resolutions[policy_id]

    @gl.public.view
    def get_history_entry(self, policy_id: str, version: u256) -> str:
        key = f"{policy_id}:{int(version)}"
        entry = self.history.get(key)
        if entry is None:
            raise gl.vm.UserError("no history entry for this policy/version.")
        return entry
