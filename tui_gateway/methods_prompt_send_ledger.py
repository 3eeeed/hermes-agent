"""``prompt.submit`` exactly-once send ledger: the client half (reservation, replay, digest).

A client that mints one immutable ``client_send_id`` per user message may retry it after a
lost response (a dropped WebSocket — exactly the socket a "Move here" resend rides) without
posting it twice. The durable record lives in ``hermes_state_send_ledger``; this module owns
the gateway-side cache, digest and reservation lifecycle that ``methods_prompt`` wraps around
the submit pipeline.

Bodies are rebound onto server.py's globals at install time (see method_ctx.bind_module), so
they reference server.py globals bare.
"""

from .method_ctx import bind_module


# How many accepted send ids one session remembers IN MEMORY. This is only a cache in
# front of the durable ledger (a retry for the current message, or a handful behind it
# after a reconnect, is answered without touching the store); the store is the authority
# and has its own, longer retention. Bounded so a long conversation cannot accumulate a
# record per turn.
_ACCEPTED_SEND_ID_LIMIT = 64


def _remembered_send_result(session: dict | None, send_id: str, digest: str) -> dict | None:
    """The acceptance this session already gave for ``send_id`` and THIS payload, if any.

    Digest-matched, not id-matched: the same id carrying a different message is a
    conflict for the ledger to refuse, never something this cache may answer."""
    if not session:
        return None
    with session["history_lock"]:
        remembered = (session.get("_accepted_send_ids") or {}).get(send_id)
    if not isinstance(remembered, dict) or remembered.get("digest") != digest:
        return None
    return dict(remembered.get("result") or {})


def _remember_send_result(session: dict | None, send_id: str, digest: str, result: dict) -> None:
    if not session:
        return
    with session["history_lock"]:
        accepted = dict(session.get("_accepted_send_ids") or {})
        accepted[send_id] = {"digest": digest, "result": dict(result)}
        while len(accepted) > _ACCEPTED_SEND_ID_LIMIT:
            accepted.pop(next(iter(accepted)))
        session["_accepted_send_ids"] = accepted


# Submit params that are not part of the MESSAGE: the runtime session id is re-minted by
# every reconnect (the ledger is keyed by the STORED id instead), ``profile`` only names
# which store to look in, ``client_send_id`` is the key itself, and ``submission_id`` is the
# canonical gateway's per-attempt admission id (re-minted by every Desktop dispatch).
_SEND_DIGEST_IGNORED_PARAMS = frozenset({
    "session_id", "profile", "client_send_id", "staged_attachment_paths", "submission_id"})


def _send_payload_digest(params: dict) -> str:
    """Canonical digest of everything this submit MEANS, or "" when it cannot be taken.

    Deny-list, not allow-list: every param that is not explicitly transient counts, so the
    text, the display kind, the truncation target and its confirmations, the rebind list,
    the surface, the voice context — and any field added later — all change the digest. An
    id may only ever replay the acceptance of the exact message it was minted for.

    ``_``-prefixed params are gateway-stamped internals (turn author, hosted-room proof)
    no client can send, and those paths carry no send id at all.

    Attached image bytes deliberately do not participate: they ride the session
    (``image.attach``). ``staged_attachment_paths`` is attempt-local evidence rather
    than message meaning and is used only to consume copies this retry really staged.
    """
    payload = {
        key: value for key, value in params.items()
        if not key.startswith("_") and key not in _SEND_DIGEST_IGNORED_PARAMS}
    try:
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        # Nothing off the wire can land here (it was JSON a moment ago). An internal caller
        # that passes an exotic value keeps the in-memory behaviour rather than failing.
        logger.warning("prompt.submit: send payload is not canonicalisable; durable dedup skipped")
        return ""
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _consume_replayed_attachments(session: dict | None, params: dict) -> None:
    """Consume image occurrences re-staged solely to retry an accepted send.

    ``image.attach*`` rides ahead of ``prompt.submit`` on every runtime. When the
    durable ledger answers a lost-response retry without entering the submit
    pipeline, those copies otherwise remain in ``attached_images`` and are picked
    up by the next, unrelated turn. The client names only paths it staged during
    THIS request; remove one newest matching occurrence per path and nothing else.
    """
    paths = params.get("staged_attachment_paths")
    if not session or not isinstance(paths, list) or not paths:
        return
    wanted = [str(path) for path in paths if isinstance(path, str) and path]
    if not wanted:
        return
    with session["history_lock"]:
        remaining = list(session.get("attached_images") or [])
        for path in wanted:
            # Staging appends. Remove the newest matching occurrence so an older,
            # separately staged same-path attachment remains owned by its turn.
            for index in range(len(remaining) - 1, -1, -1):
                if remaining[index] == path:
                    remaining.pop(index)
                    break
        session["attached_images"] = remaining


def _send_reservation_holder(sid: str) -> str:
    """Who holds a reservation. The ``pid=`` tail is load-bearing: a reservation whose
    process is provably gone is reclaimable, the same kernel-proof rule the turn lease
    uses — a backend killed mid-submit must not wedge the user's message forever."""
    from hermes_state_pidns import holder_namespace_token
    from hermes_state_send_ledger import prompt_send_host_identity
    # ``pidns=`` stamp: upstream only probes a holder's pid in a namespace it can prove is ours.
    return (f"submit:{sid or '-'}:host={prompt_send_host_identity()}:pid={os.getpid()}"
            f"{holder_namespace_token()}")


class _SendReservation:
    """This process's claim on one (stored session, client send id), held from before the
    first mutation until the submit has an answer.

    A row bind or acceptance is required durable state. The guarded worker is not
    exposed until ``accept`` commits, so write failure can release safely and retry.
    """

    def __init__(self, session: dict, key: str, send_id: str, holder: str, user_row_id):
        self.session, self.key, self.send_id, self.holder = session, key, send_id, holder
        #: Durable user row a previous attempt at this send left in the transcript.
        self.user_row_id = user_row_id
        self.accepted = False

    def record_user_row(self, user_row_id) -> None:
        """Stamp the row this attempt just wrote, as soon as it lands — if the backend dies
        before the acceptance, the retry adopts it instead of posting the message twice."""
        if not isinstance(user_row_id, int) or user_row_id == self.user_row_id:
            return
        self._write_required(lambda db: db.record_prompt_send_user_row(
            self.key, self.send_id, user_row_id, holder=self.holder))
        self.user_row_id = user_row_id

    def accept(self, result: dict) -> None:
        if self.accepted:
            return
        self._write_required(lambda db: db.accept_prompt_send(
            self.key, self.send_id, result, holder=self.holder))
        self.accepted = True

    def bind_accepted_row(self, user_row_id) -> None:
        """Best-effort: the busy-queue row lands after acceptance; remember it for retries."""
        if isinstance(user_row_id, int) and self.accepted:
            self._write(lambda db: db.bind_accepted_prompt_send_row(self.key, self.send_id, user_row_id))
            self.user_row_id = user_row_id

    def release(self) -> None:
        self._write(lambda db: db.release_prompt_send(self.key, self.send_id, holder=self.holder))

    def _write(self, call) -> None:
        try:
            with _session_db(self.session) as db:
                if db is not None:
                    call(db)
        except Exception:
            logger.warning(
                "prompt.submit: send ledger write failed for %s/%s", self.key, self.send_id,
                exc_info=True)

    def _write_required(self, call) -> None:
        """A row stamp/acceptance is part of the exactly-once proof, not logging.

        Returning success after either write failed leaves only process memory as
        evidence and permits a restarted backend to post or run the send again.
        """
        with _session_db(self.session) as db:
            if db is None or call(db) is not True:
                raise RuntimeError("durable prompt-send ledger transition was not applied")


# The terminal answer to a send that provably started no turn: whatever the gateway had
# already done for it has been given back, so the only honest thing to tell the client is
# that no reply is coming and the message is theirs to send again.
_UNSTARTABLE_SEND_MSG = (
    "This message could not be started, so no reply is coming: {exc}. Send it again.")

_SEND_LEDGER_UNAVAILABLE_MSG = (
    "This message was not sent: the gateway could not record it as sent exactly once, so it "
    "refused to risk posting it twice. Send it again.")


def _open_send_reservation(rid, session: dict | None, send_id: str, digest: str):
    """Claim this send BEFORE anything is mutated: ``(reservation, replay, err)``.

    At most one of the three is set; all three None means "no durable identity here" —
    an unsaved draft has no stored session to key a ledger on, and a context with no store
    keeps the in-memory behaviour exactly as it was.

    ``replay`` is the answer a duplicate gets: the acceptance already given, or — for a
    request still in flight under a live holder — a deterministic accepted one, which is
    what the client needs to stop holding the message. ``err`` is either fail-closed (this
    id belongs to a different message) or retryable (the store could not answer, and
    because the reservation comes first, nothing has happened yet).
    """
    key = str((session or {}).get("session_key") or "")
    if not session or not key or not digest:
        return None, None, None
    holder = _send_reservation_holder(str(session.get("_sid") or ""))
    try:
        with _session_db(session) as db:
            verdict = None if db is None else db.reserve_prompt_send(
                key, send_id, digest, holder=holder)
    except Exception as exc:
        logger.warning(
            "prompt.submit: send ledger unavailable for %s/%s: %s", key, send_id, exc,
            exc_info=True)
        return None, None, _err(rid, 5074, _SEND_LEDGER_UNAVAILABLE_MSG, data={"retryable": True})
    if verdict is None:
        return None, None, None
    if verdict.outcome == "conflict":
        logger.warning(
            "prompt.submit: REFUSED send_id=%s on session %s — it was already used for a "
            "different message", send_id, key)
        return None, None, _err(
            rid, 4134,
            "this client_send_id was already used for a different message; "
            "mint a new one per message and reuse it only to retry that message",
            data={"client_send_id": send_id})
    if verdict.outcome == "accepted":
        return None, dict(verdict.result or {"status": "streaming"}), None
    if verdict.outcome == "in_progress":
        # The holder has not accepted yet and may still fail/release. Reporting
        # success here would make the overtaking client discard the only preserved
        # copy of a message for which no turn is guaranteed.
        return None, None, _err(
            rid, 4133, "this send is still being accepted; retry it unchanged",
            data={"client_send_id": send_id, "retryable": True})
    return _SendReservation(session, key, send_id, holder, verdict.user_row_id), None, None


def register(server) -> None:
    """Publish this module's helpers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
