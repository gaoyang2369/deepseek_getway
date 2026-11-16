-- Execute the authoritative admission in one server round trip. No prompt,
-- response text or plaintext credential is passed to this function.
-- SECURITY INVOKER: this function does not grant additional database privileges.
CREATE OR REPLACE FUNCTION gateway_admit(
    key_digest text, requested_model text, requested_output integer,
    input_bound bigint, request_id text, source_ip text,
    team_attempts bigint DEFAULT 0, member_attempts bigint DEFAULT 0,
    admission_time timestamptz DEFAULT NULL
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    k personal_keys%ROWTYPE;
    m memberships%ROWTYPE;
    s students%ROWTYPE;
    t teams%ROWTYPE;
    price prices%ROWTYPE;
    credential credentials%ROWTYPE;
    p budget_periods%ROWTYPE;
    c calls%ROWTYPE;
    at_time timestamptz;
    period_start timestamptz;
    period_end timestamptz;
    member_period text;
    team_period text;
    output_limit integer;
    reserve_amount bigint;
    active_team bigint;
    active_member bigint;
    recent_team bigint;
    recent_member bigint;
    seen boolean;
    source_dedup text;
BEGIN
    SELECT * INTO k FROM personal_keys WHERE digest = key_digest;
    IF NOT FOUND THEN RETURN jsonb_build_object('status',401,'error','invalid_api_key'); END IF;
    SELECT * INTO m FROM memberships WHERE id = k.membership_id;
    -- Same mutex as administrative changes. Hold it only during server-side work.
    SELECT * INTO t FROM teams WHERE id = m.team_id FOR NO KEY UPDATE;
    -- Separate statements see changes committed while the team lock was awaited.
    SELECT * INTO k FROM personal_keys WHERE digest = key_digest;
    SELECT * INTO m FROM memberships WHERE id = k.membership_id;
    SELECT * INTO s FROM students WHERE id = m.student_id;
    at_time := coalesce(admission_time,clock_timestamp());
    IF NOT k.active OR (k.expires_at IS NOT NULL AND k.expires_at <= clock_timestamp()) THEN
        RETURN jsonb_build_object('status',401,'error','invalid_api_key');
    END IF;
    IF NOT m.active OR NOT s.active OR NOT t.active THEN
        RETURN jsonb_build_object('status',403,'error','identity_disabled');
    END IF;
    IF NOT (t.models ? requested_model AND m.models ? requested_model AND k.models ? requested_model) THEN
        RETURN jsonb_build_object('status',403,'error','model_not_allowed');
    END IF;
    IF team_attempts > t.rpm OR member_attempts > m.rpm THEN
        RETURN jsonb_build_object('status',429,'error','rate_limit','limit_scope', CASE WHEN team_attempts > t.rpm AND member_attempts > m.rpm THEN 'both' WHEN team_attempts > t.rpm THEN 'team' ELSE 'membership' END);
    END IF;
    SELECT * INTO price FROM prices WHERE model = requested_model AND starts_at <= at_time
        AND (ends_at IS NULL OR ends_at > at_time) ORDER BY starts_at DESC LIMIT 1;
    IF NOT FOUND THEN RETURN jsonb_build_object('status',503,'error','price_not_configured'); END IF;
    SELECT * INTO credential FROM credentials WHERE team_id = t.id AND active
        ORDER BY version DESC LIMIT 1;
    IF NOT FOUND THEN RETURN jsonb_build_object('status',503,'error','team_upstream_unavailable'); END IF;
    output_limit := least(coalesce(requested_output,t.max_output),t.max_output);
    reserve_amount := ceil((input_bound::numeric * price.input_miss
        + output_limit::numeric * price.output) / 1000000)::bigint;

    period_start := date_trunc(m.period,at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai';
    period_end := (period_start AT TIME ZONE 'Asia/Shanghai'
        + CASE m.period WHEN 'month' THEN interval '1 month' ELSE interval '1 day' END) AT TIME ZONE 'Asia/Shanghai';
    SELECT id INTO member_period FROM budget_periods
        WHERE owner_type='membership' AND owner_id=m.id AND starts_at=period_start;
    IF NOT FOUND THEN
        INSERT INTO budget_periods(id,owner_type,owner_id,starts_at,ends_at,policy_version,"limit",spent,reserved)
            VALUES(gen_random_uuid()::text,'membership',m.id,period_start,period_end,m.policy_version,m.budget,0,0)
            RETURNING id INTO member_period;
    END IF;
    period_start := date_trunc(t.period,at_time AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai';
    period_end := (period_start AT TIME ZONE 'Asia/Shanghai'
        + CASE t.period WHEN 'month' THEN interval '1 month' ELSE interval '1 day' END) AT TIME ZONE 'Asia/Shanghai';
    SELECT id INTO team_period FROM budget_periods
        WHERE owner_type='team' AND owner_id=t.id AND starts_at=period_start;
    IF NOT FOUND THEN
        INSERT INTO budget_periods(id,owner_type,owner_id,starts_at,ends_at,policy_version,"limit",spent,reserved)
            VALUES(gen_random_uuid()::text,'team',t.id,period_start,period_end,t.policy_version,t.budget,0,0)
            RETURNING id INTO team_period;
    END IF;
    -- Team budget before member budget, also used by settlement. This avoids
    -- cycles when a batch admits several members while older calls settle.
    FOR p IN SELECT * FROM budget_periods WHERE id IN (member_period,team_period) ORDER BY owner_type DESC,id FOR UPDATE LOOP
        IF p."limit" - p.spent - p.reserved < reserve_amount THEN
            RETURN jsonb_build_object('status',402,'error','budget_exhausted','limit_scope',p.owner_type);
        END IF;
    END LOOP;

    -- Keep the active and sliding-minute scans independent, with bounded indexes.
    SELECT count(*),count(*) FILTER(WHERE membership_id=m.id) INTO active_team,active_member
        FROM calls WHERE team_id=t.id AND status IN ('reserved','running','pending');
    IF active_team >= t.concurrency OR active_member >= m.concurrency THEN
        RETURN jsonb_build_object('status',429,'error','concurrency_limit','limit_scope', CASE WHEN active_team >= t.concurrency AND active_member >= m.concurrency THEN 'both' WHEN active_team >= t.concurrency THEN 'team' ELSE 'membership' END);
    END IF;
    SELECT count(*),count(*) FILTER(WHERE membership_id=m.id) INTO recent_team,recent_member
        FROM calls WHERE team_id=t.id AND started_at >= at_time - interval '60 seconds';
    IF recent_team >= t.rpm OR recent_member >= m.rpm THEN
        RETURN jsonb_build_object('status',429,'error','rate_limit','limit_scope', CASE WHEN recent_team >= t.rpm AND recent_member >= m.rpm THEN 'both' WHEN recent_team >= t.rpm THEN 'team' ELSE 'membership' END);
    END IF;
    SELECT EXISTS(SELECT 1 FROM calls WHERE membership_id=m.id AND ip=source_ip) INTO seen;

    -- 'reserved' means durable admission, possibly dispatched. Recovery treats
    -- it as uncertain; no second pre-dispatch 'running' commit is needed.
    INSERT INTO calls(id,student_id,membership_id,team_id,key_id,credential_id,price_id,
        member_period_id,team_period_id,model,status,reservation,attempt,ip,started_at)
        VALUES(request_id,s.id,m.id,t.id,k.id,credential.id,price.id,member_period,team_period,
            requested_model,'reserved',reserve_amount,1,source_ip,at_time) RETURNING * INTO c;
    UPDATE budget_periods SET reserved=reserved+reserve_amount WHERE id IN (member_period,team_period);
    INSERT INTO ledger(id,operation,period_id,call_id,kind,amount,reason,created_at)
        VALUES(gen_random_uuid()::text,'reserve:'||request_id||':'||member_period,member_period,
            request_id,'reserve',reserve_amount,'admission',at_time),
        (gen_random_uuid()::text,'reserve:'||request_id||':'||team_period,team_period,
            request_id,'reserve',reserve_amount,'admission',at_time);
    IF NOT seen THEN
        source_dedup := encode(sha256(convert_to('new_source:'||t.id||':'||m.id||':'||source_ip,'UTF8')),'hex');
        INSERT INTO events(id,dedup,kind,team_id,membership_id,detail,resolved,created_at,last_seen_at)
            VALUES(gen_random_uuid()::text,source_dedup,'new_source',t.id,m.id,'{}',false,at_time,at_time)
            ON CONFLICT(dedup) DO NOTHING;
    END IF;
    RETURN jsonb_build_object('call',to_jsonb(c),'ciphertext',credential.ciphertext,
        'upstream_model',price.upstream_model,'max_tokens',output_limit);
END;
$$;

-- A subtransaction rolls back each rejected item, while accepted items share a
-- durable commit. No caller receives a successful result before that commit.
CREATE OR REPLACE FUNCTION gateway_admit_batch(items jsonb)
RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
    item jsonb;
    result jsonb;
    results jsonb := '[]'::jsonb;
    -- One calendar/price instant per batch, recorded on every Call. Do not hold
    -- budgets for two different calendar periods within a single batch.
    batch_time timestamptz := clock_timestamp();
BEGIN
    FOR item IN SELECT value FROM jsonb_array_elements(items) LOOP
        BEGIN
            result := gateway_admit(item->>'key_digest',item->>'model',
                (item->>'output')::integer,(item->>'input_bound')::bigint,
                item->>'request_id',item->>'ip',
                coalesce((item->>'team_attempts')::bigint,0),coalesce((item->>'member_attempts')::bigint,0),batch_time);
            IF result ? 'error' THEN
                RAISE EXCEPTION USING ERRCODE='P0001', MESSAGE='admission_rejected';
            END IF;
        EXCEPTION WHEN SQLSTATE 'P0001' THEN
            -- PL/pgSQL variables keep their values while database changes roll back.
            NULL;
        END;
        results := results || jsonb_build_array(result);
    END LOOP;
    RETURN results;
END;
$$;
