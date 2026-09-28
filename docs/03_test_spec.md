# Test Specification — GOV-C2-110

> Municipal Procurement Contract-Variation Evidence Brief Agent
> 設計: `docs/02_design.md`

## Test Strategy

- **実行**: repo ルートで `pytest -q`（`tests/` に閉じない。`scripts/` の scaffold 自己回帰テストを含む）
- **構成**: unit（ノード契約・S-1/S-2/S-3・閉じた列挙）/ integration（実グラフ + 実 HTTP アダプタ）/
  proof_of_boundary（フレームワーク境界・egress チャネル）
- **本番 config で検証する**: `agent_config` fixture は `config/config.yaml` と同じ形。
  ハンドメイド state だけで通るゲートは「実装した」ことにならない。
- **現状**: **258 passed / 2 skipped**（skip = PB-7・非 HITL のため auto-waived）

## Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State contract: flat TypedDict | Pydantic/dataclass なし・msgpack 可 | ✅ `test_pb_egress_channels.py::test_state_is_msgpack_friendly` / `test_state_safety.py` |
| TC-02 | SecurityViolationError fires on invalid input | 不正入力で raise | ✅ `test_nodes.py::TestPreProcessS2Gate`（My Number / 機密区分 / APPI / PDF / 未知 mode） |
| TC-03 | No JWT/Credential in State | `gate-credential-scan` 0 violations | ✅ CI + `test_pb_egress_channels.py::TestNoCredentialsInState` |
| TC-04 | InvocationContext via configurable only | 直接構築しない・`from_state` 使用 | ✅ `FeedbackNode`（不完全 context は fail-closed） |
| TC-05 | S-4: no duplicate lifecycle events in `execute()` | `node_start`/`node_complete`/`node_error` を execute 内で emit しない | ✅ 全ノードでドメインイベントのみ |
| TC-06 | S-2 `_security_gate_input()` 非バイパス | `@final`・`_extra_*` フックで拡張 | ✅ `test_framework_compliance_tc06_tc07.py`（scaffold 提供） |
| TC-07 | S-3 `_security_gate_output()` 非バイパス | 同上 | ✅ 同上 |
| TC-08 | `required_trust_level` 宣言と強制 | trust 不足で **execute 到達前**に拒否 | ✅ `test_nodes.py::TestTrustDeclarations`（`node(state)` 経由で execute 未実行を証明。**GraphNode も対象**）＋ PB-6 `TestInvokeOrder::test_s1_denial_refuses_execution_before_execute` が `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / **`execute()` 未呼び出し** を assert（正本 docs/03 の TC-08 要求） |
| TC-09 | S-2 `_extra_security_gate_input()` が非自明 | ドメイン検査が動作 | ✅ IMP-76 規制データ検知 + 入力形式 + mode allowlist |
| TC-10 | S-3 `_extra_security_gate_output()` が非自明 | ドメイン検査が動作 | ✅ 出力構造 allowlist + claim→source + span_kind + 入れ子 PII |
| TC-11 | S-4: 各 `execute()` に最低 1 つのドメインイベント | 全経路で emit | ✅ `test_pb_egress_channels.py::TestAuditLogEgress` |

## Proof-of-Boundary Tests

| PB-ID | Boundary | Result |
|---|---|---|
| PB-1 | BaseNode → AuditLogger | ✅ `TestAuditLogEgress`（emit されること + 中身が allowlist 内であること） |
| PB-2 | State serialization | ✅ `test_state_safety.py` / `test_state_is_msgpack_friendly` |
| PB-3 | L1 → External service | ⚠️ **未配線**（KB/DB は注入シームのみ。下記「既知の限界」参照） |
| PB-4 | Import isolation | ✅ `test_import_isolation.py`（AST scan 0 violations） |
| PB-5 | Checkpoint safety | ✅ `test_pb_egress_channels.py::TestCheckpointStateSafety`（**実 InMemorySaver** で検証） |
| PB-6 | Invoke execution order | ✅ `test_pb_invoke_order.py`（全ノード。**2 件の実欠陥を検出し修正済** — 下記） |
| PB-7 | HITL interrupt propagation | **auto-waived — 非 HITL**（`config/config.yaml` に `hitl.enabled` なし。スタブは scaffold 原本のまま） |

> **PB-6 が捕まえた実欠陥**: `ContractVariationGraphNode` と `FeedbackNode` が不完全 state
> （conformance probe が渡す 2 キーのみ）で `KeyError: 'session_id'` を送出していた。
> 例外ではなく fail-closed の error delta を返すよう修正。**合成 state での probe が本物の
> 堅牢性欠陥を暴いた例**であり、テスト都合の回避はしていない。

## 本テンプレ固有の受入テスト（安全境界）

| # | 検証内容 | テスト |
|---|---|---|
| A-1 | **adjudicative span は引用できない** — 証跡自体に「本変更は約款上許容される」があっても引用させない | `test_nodes.py::TestStage4::test_adjudicative_span_cannot_be_quoted` / `test_end_to_end.py::test_adjudicative_source_cannot_be_quoted_out` |
| A-2 | 分類不能 span（`unclassified`）は既定で引用不可（fail-closed） | `test_span_kind_and_contracts.py::test_unrecognised_text_fails_closed` |
| A-3 | 結論を含む文は事実的に見えても adjudicative に倒れる | `test_conclusive_marker_wins_over_factual_content` |
| A-4 | **結論フィールド 6 種は出力スキーマで reject** | `test_conclusion_fields_are_rejected`（parametrize 6 件）/ `test_injected_conclusion_field_never_reaches_the_caller` |
| A-5 | 許可フィールドの**入れ子自由文**も reject（キー名 allowlist だけでは閉じない） | `test_free_text_field_inside_an_element_is_rejected` |
| A-6 | **縮退経路の negative test** — `review_reason_code` / `gap_codes` に法的結論文字列を入れて reject | `TestDegradationContract::test_free_text_reason_code_is_rejected` ほか |
| A-7 | 捏造 source ID / 引用改変を reject | `test_fabricated_source_is_refused` / `test_altered_quote_is_refused` |
| A-8 | 版不整合で意味的照合に進まず fail-closed | `TestStage1::test_version_mismatch_stops_before_semantic_work` |
| A-9 | **ログを egress として検査** — 原文・ドラフト・引用・例外文がログに出ない | `TestAuditLogEgress`（2 件） |
| A-10 | **エラー経路に例外文・スタックトレース・入力エコーが出ない** | `TestErrorEnvelopeEgress` / `test_failures_never_leak_exception_text` |
| A-11 | 承認者権限は **verified context から解決**（body の自己申告を無視） | `TestFeedbackAuthority::test_approver_comes_from_context_not_the_request_body` |
| A-12 | feedback の自由文は KB に射影しない | `test_free_text_is_not_written_to_the_kb` |
| A-13 | 過去決裁・結論ラベルは検索セーフ射影から除外 | `TestRetrievalSafeProjection` |
| A-14 | 引用不可 span は **プロンプトにも載せない** | `test_non_quotable_spans_are_withheld_from_the_prompt` |
| A-15 | `/invoke` `/ingest` `/feedback` すべてに入口認証 | `test_all_write_routes_require_the_entry_token` |
| A-16 | **`/resume` は未登録**（非 HITL） | `test_resume_endpoint_is_not_registered` |
| A-17 | LLM 未設定・LLM 例外時はドラフトを出さず縮退 | `test_missing_llm_withholds_rather_than_inventing` / `test_provider_exception_text_is_not_propagated` |

## 既知の限界（正直な自己申告）

後段のステージング検証・独立レビューの計画になるよう、通っていないものを明示する。

1. **PB-3（実外部サービス接続）は未達**。KB/DB は注入シーム（`kb` / `llm` 引数）のみで、実 DB・実ベクトル
   ストアへは未接続。テストは `CapturingKB` ダブルを使う。実接続は導入時に顧客専用接続を provision する前提。
2. **実 LLM 未検証**。`FakeLLM` による決定論テストのみ。実 LLM が schema を外れた応答を返す頻度・
   引用改変の傾向は STG 実測が必要。
3. **`span_kind` 分類の網羅性は原理的に不完全**。日本語の言い回しは無限にあり、マーカー方式で
   adjudicative を全部捕まえることはできない。**だからこそ `unclassified` を引用不可の既定**にしている
   （分類漏れは「引用されない」側に倒れる）。逆に、事実的な span が `unclassified` に落ちて
   引用されない過検知は起こりうる — その場合は `gaps` に出るので人間が気づける。
4. **並行実行未検証**。ノードは state 経由でのみデータを流し可変インスタンス変数を持たないが、
   実際の並行負荷試験は未実施。
5. **`detect_pii` の日本語欠落**（既知の SDK 制約）。既定方針に従い自前の日本語補完サニタイザは
   実装していない。AG は多層防御の一枚であり、PII 統制は顧客側の運用と併用される前提。
