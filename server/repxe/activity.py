"""The effective routine outcome used by counts, filters and detail views."""
TERMINAL = ('complete', 'failed', 'cancelled')
CHECKPOINTS = ('checkpoint', 'custom-boot')
ATTENTION = ('failed', 'needs-operator')


def routine_stage(stage, client_stage=None, job_stage=None, has_report=False, erase_stage=None, erase_stale=False):
    # Historical child state must never reopen a completed/cancelled routine.
    if stage in TERMINAL:
        return stage
    if stage in CHECKPOINTS or (stage == 'erase-running' and (erase_stage == 'operation-pending' or erase_stale)):
        return 'needs-operator'
    if stage in ('windows-boot', 'windows-running') and client_stage == 'waiting-for-input':
        return 'needs-operator'
    if stage == 'windows-running':
        if job_stage in TERMINAL or has_report:
            return job_stage or stage
        return client_stage or job_stage or stage
    return stage


def routine_stage_sql(erase_stale=False):
    """Equivalent SQL for the r/c/j/w evidence joins; covered by parity tests."""
    stale = " OR (r.stage='erase-running' AND w.stage='erasing' AND julianday('now')-julianday(COALESCE((SELECT updated FROM workflow_progress WHERE run_id=w.id),w.updated))>60.0/86400)" if erase_stale else ''
    return """CASE WHEN r.stage IN ('complete','failed','cancelled') THEN r.stage
        WHEN r.stage IN ('checkpoint','custom-boot') OR
          (r.stage='erase-running' AND w.stage='operation-pending')
          """+stale+""" OR
          (r.stage IN ('windows-boot','windows-running') AND c.stage='waiting-for-input') THEN 'needs-operator'
        WHEN r.stage='windows-running' THEN CASE
          WHEN j.stage IN ('complete','failed','cancelled') OR j.report IS NOT NULL THEN COALESCE(j.stage,r.stage)
          ELSE COALESCE(c.stage,j.stage,r.stage) END ELSE r.stage END"""
