# ─────────── BULK REFILL CYCLE ───────────
def run_cycle() -> None:
    moved = handler.bulk_refill(user=USER)
    st.session_state.last_refilled_count = moved
    log_entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "moved": moved,
    }
    st.session_state.history_log.append(log_entry)
    if moved:
        st.session_state.refilled_log.append(log_entry)

# ─────────── main loop ───────────
if st.session_state.running:
    now = time.time()
    rem = SECONDS - (now - st.session_state.last_ts)
    if rem <= 0:
        run_cycle()
        st.session_state.cycles += 1
        st.session_state.last_ts = time.time()
        rem = SECONDS

    cc1, cc2, cc3 = st.columns(3)
    cc1.metric("Cycles", st.session_state.cycles)
    cc2.metric("Rows moved", st.session_state.last_refilled_count)
    cc3.metric(
        "Last run",
        datetime.fromtimestamp(st.session_state.last_ts).strftime("%F %T"),
    )
    st.progress(1 - rem / SECONDS,
                text=f"Next cycle in {int(rem)} s")
    time.sleep(0.15)
    st.rerun()
else:
    st.info("Press **Start** to begin automatic shelf top‑ups.")
