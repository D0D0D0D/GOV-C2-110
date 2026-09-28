# Template Design Specification — GOV-C2-110

> Municipal Procurement Contract-Variation Evidence Brief Agent
> （自治体調達の契約変更 証拠ブリーフ）
>

## Position in AgentCore Architecture

- **Agent Class**: `ContractVariationEvidenceBriefAgent`（`src/graph/graph.py`・outer graph）
- **L1 Base**: **AgentBaseGraph**（L1 直接継承）。DocGenerationAgent パターン。
  - 旧 Level 2 base 継承モデルは廃止され、本テンプレートは L1 backbone を直接継承する
    （移行注記: 旧「Level 2 base 継承」→「L1 直接継承」）。
- **Three-Layer Separation**:
  - State: flat TypedDict composition（Pydantic 不可 — msgpack 非互換）
  - Node: L1 継承（Template Method: `execute(self, state) -> dict` のみ override・**部分 dict 返却**）
  - Graph: composition（`register_nodes()` でノード差し替え、`add_edges()` / `route()` は override しない）

本テンプレートは **Cat 2**。外側 backbone は Cat 1 と同一の固定 5 ノードで、ドメインの多段ロジックは
`main` スロットに割り当てた `GraphNode`（内側 domain workflow graph）に隠蔽する。

## Architecture Overview

### 3 実行モード

| Mode | エントリ | 目的 | 主な書込 |
|------|---------|------|---------|
| `ingest` | `POST /ingest` | 承認済みベースライン契約・約款条項・権限表・**人間が事前承認した事実確認観点**・過去の変更レビュー観点を構造化して DB / KB に蓄積。**約款許容性・承認・適法性の結論ラベルは検索軸・モデル payload から除外**する。 | `baseline_contracts` / `clause_refs` / `authority_matrix` / `review_perspectives`（DB）、承認済み観点の検索セーフ射影（KB） |
| `invoke` | `POST /invoke` | 新規の契約変更案件について、変更理由・変更設計・積算・工程影響を承認済み事実確認観点に照合し、証拠ブリーフのドラフト＋根拠マップを生成。 | なし（読み取り + ドラフト生成）。低信頼・版不整合時は `evidence_brief` を返さず `requires_human_review` + 不足コードで正常終了（再呼び出し） |
| `feedback` | `POST /feedback` | 人間（決裁権者・起案者）が編集・確定した観点を、明示承認＋監査イベント付きで DB / KB に反映。 | `feedback_audit_events`（DB）、承認済み観点（KB） |

`mode` は `state["mode"]`（`validated_input` に含める）で分岐し、内側 domain workflow graph が
条件辺でルーティングする。

### AGENTIC STAR Marketplace entry adapter（`cli.py`）

Marketplace runner は最新メッセージに加えて
`input_context={"conversation_history": [...]}` を渡すが、この履歴は `PreProcessNode` の S-2 が走査する
`user_input` ではない。そこで `MarketplaceGraph.invoke()` は受け取った `input_context` を破棄し、空 dict から
組み直す。会話履歴や将来 runner が追加する未知キーを State に載せない。

Marketplace 経路は読み取り専用である。adapter が deployment policy として `mode="invoke"` を設定し、
`ingest` / `feedback` その他の mode 指定は入口で拒否する。入力は JSON object とし、案件固有の必須
`case_ref` は空・空白・opaque identifier 規則違反を fail closed にする。ユーザーから受理するのは
`case_ref` / `variation_rationale` / `evidence` / `versions` / `authority` だけで、書込用の `records` /
`confirmed_perspectives` は State に載せない。この AG の State には tenant/org scope フィールドがないため、
架空の scope は追加しない（自治体分離は deployment ごとの専用接続で行う）。

`MarketplaceGraph` は既存 `ContractVariationEvidenceBriefAgent` を継承し、LLM 注入シームだけを
`MarketplaceAzureLLM` に差し替える。Azure の API key / endpoint / deployment はインスタンスに保持せず、
各 `complete()` / `stream()` ごとに secret context から解決して新しい client を生成する。
`AzureOpenAIClient` は呼出し時にのみ import し、SDK 1.0.0 / 1.0.1 に存在しない場合は同じ Azure Chat
Completions 契約の互換 client を使う。`provision_secrets()` は runner の広い provider を採用せず、環境変数
allowlist の Azure 3 値を起動時に検査する。欠落・空・空白混入はいずれも起動失敗である。

この adapter は Stage 1 / 2 / 4 の決定論的検証や S-3 を変更しない。Azure に寄せるのは Stage 3 の
「既存 perspective/source handle の選択」だけであり、変更の許容性・適法性・妥当性・承認判断を LLM に
委ねるものではない。

### Node Configuration（outer backbone — 固定 5 ノード）

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| initialize | セッション初期化（framework 既定） | user_input | session_id, node_history | InitializeNode (default) |
| pre_process | 顧客スコープ解決（fail-closed）・入力検証・S-2 情報最小化・mode 判定・案件参照 ID 化 | user_input, invocation ctx | validated_input, mode, case_ref | `PreProcessNode(FunctionNode)` |
| main | `GraphNode` として内側 domain workflow graph を呼ぶ（Stage 1〜4 / mode 分岐） | validated_input, mode, case_ref | evidence_brief, evidence_map, gaps, requires_human_review, degradation, version_check, status | `ContractVariationGraphNode(GraphNode)` |
| post_process | S-3 出力検査（**出力構造 allowlist**・claim→source 実在検証・PII/credential 再走査）・出力整形。`requires_human_review` 時は縮退エンベロープのみ検査 | evidence_brief, evidence_map, requires_human_review, degradation | result（整形済ブリーフ or 縮退コード）, status | `PostProcessNode(FunctionNode)` |
| finalize | 出力確定（framework 既定） | result | final_output | FinalizeNode (default) |

### 内側 domain workflow graph（`src/graph/domain_workflow_graph.py`）

`ContractVariationGraphNode.get_subgraph()` が返す内側グラフ。mode により経路が分岐する。

**内側グラフの基底と実装契約（SDK 1.0.0 実機確認）**: `ContractVariationWorkflowGraph(BaseGraph)`。
`AgentBaseGraph` は固定 5 ノード backbone を強制するため、Stage 1〜4 + mode 分岐という独自トポロジには
`BaseGraph` を直接継承する（同型 = Financial Regulatory Consultation Response Evidence Brief Agent `ConsultationBriefWorkflowGraph(BaseGraph)`）。
`BaseGraph` の `@abstractmethod` は **7 つすべて実装が必須**（未実装は `TypeError` で instantiate 不可）:

| 実装 | 内容 |
|---|---|
| `name` | `"contract_variation_workflow"` |
| `state_schema` | 内側の flat TypedDict（外側 `State` の部分集合 + Stage 中間結果） |
| `_validate_config` | `llm` / DB・KB ハンドル / `top_k` / しきい値の存在検証 |
| `register_nodes` | Stage 1〜4 + `IngestNode` + `FeedbackNode` を登録 |
| `add_edges` | mode 条件辺 + Stage 1→2→3→4 の直列辺 |
| `route` | mode 分岐と `status` による早期終了。**分岐関数の `state` は内側の自 schema で型注釈する**（`AgentState` 注釈だとカスタム欄が消えて誤 route する） |
| `get_output` | 内側の最終 state から `evidence_brief` / `evidence_map` / `requires_human_review` / **`degradation`** / `ingest_result` / `feedback_result` / `status` を取り出す |

`ContractVariationGraphNode.__init__` で 1 度だけ構築し、**`compile()` を呼んでから**保持する
（未 compile のまま返すと invoke 時に失敗する）。HITL 非採用のため checkpointer は渡さない。

```
                         ┌─ mode=ingest ──→ [IngestNode] ─────────────────────→ END
[inner initialize] → {route by mode} ─┼─ mode=feedback → [FeedbackNode] ──────────────────→ END
                         └─ mode=invoke ─→ [Stage1 DeterministicCheckNode]
                                              → [Stage2 RecallNode（承認済み観点の想起）]
                                              → [Stage3 MappingNode（LLM 当てはめ・核）]
                                              → [Stage4 EvidenceVerifyNode（claim→source 検証）]
                                              → END
```

| Inner Node | 主要ロジック | 出力 |
|---|---|---|
| Stage 1 `DeterministicCheckNode` | 必須証跡の有無・書式・**権限表との決定論照合**（config の必須証跡表・専決権限表）。意味的結論は出さない。**供給証跡から `source_registry`（source_id → doc_ref/span_ref/`span_kind`/provenance）を採番**し、各 span に**引用可否を決める `span_kind` を決定論的に付与**する（分類不能は `unclassified` = 引用不可の fail-closed）。**版整合（`contract_version` / `authority_matrix_version` / effective date）を検証し、不一致・欠落なら以降の意味的照合をせず `unknowns` / `verification_questions` へ fail-closed**（レビュー所見への対応） | `docs_present`, `format_check`, `authority_match`, `gaps`(構造欠落), `source_registry`, `version_check` |
| Stage 2 `RecallNode` | DB を顧客専用接続で検索（未解決は fail-closed）、KB は**検索セーフ射影**（source-linked な承認済み事実確認観点・source ID・provenance）だけを検索。**約款許容性・承認・適法性の結論は継承しない**。 | `recalled_perspectives`, `prior_ids` |
| Stage 3 `MappingNode`（**LLM 核**） | 変更理由・変更設計（自由記述）と積算・工程影響を、**人間が事前承認した事実確認観点**に当てはめ、記載事実・証跡対応・不足・矛盾・確認質問を抽出。**事実と推論を分離**。**source は `source_registry` の既存ハンドルの選択のみ可（LLM に source ID を創作させない）**。schema 出力。 | `evidence_brief`(draft) |
| Stage 4 `EvidenceVerifyNode` | 全論点に source link 必須。**`evidence_map` は `source_registry` から決定論的に構築**し、LLM が付けた source が registry に**実在するか**＋claim→source 対応を検証。未知 ID / 不一致は reject。低信頼・版不整合・claim→source 不整合のいずれでも `evidence_brief` を返さず `requires_human_review: true` + **`degradation` エンベロープ**（閉じた列挙のみ）で fail-closed（グラフ内 suspend はしない＝再呼び出し方式）。 | `evidence_map`, `requires_human_review`, `degradation` |
| `IngestNode` | 承認済み契約・約款・権限表・事実確認観点を構造化品質フラグ付きで DB 蓄積、KB は**許可フィールドのみ** projection。 | `ingest_result` |
| `FeedbackNode` | 人間確定観点を明示承認 + 監査イベント付きで反映。 | `feedback_result` |

**実装上の State 読み取り**: outer `PreProcessNode` は `user_input` と `input_context.mode/case_ref`、
`ContractVariationGraphNode` は `mode` / `case_ref` / `validated_input`、`PostProcessNode` は `status` /
`mode` / `requires_human_review` と mode 別結果を読む。inner `UnpackInputNode` は親から直列化された同 3
フィールドを復元し、Stage 1 は `validated_input`、Stage 2 は `requires_human_review` / `case_ref`、Stage 3 は
`validated_input` / `recalled_perspectives` / `source_registry`、Stage 4 はそれらの生成結果に加えて
`docs_present` / `authority_match` / `degradation` を読む。したがって generic runner の自由な
`input_context` をそのまま渡す構成では Marketplace 境界を満たさず、上記 adapter が必要である。

> Stage 3 が唯一の LLM ノード。証跡有無・書式・権限照合・版整合（Stage 1）と根拠検証（Stage 4 の決定論部分）は
> LLM に依存しない。**LLM に事実文字列（source ID・法的結論）を創作させない**設計。

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                            ↓ (retry, max 3)
                                          pre_process
```

`route()` は override しない（framework 既定：main 後に `state["status"]` を読み success→post / retry→pre / error→finalize）。

### State Definition（`src/schemas/state.py` — `State(AgentState)`）

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | 実行モード（`ingest`/`invoke`/`feedback`）。`pre_process.execute()` が raw 入力から判定して返す | ✅ |
| `case_ref` | `str` | 契約変更案件の**参照 ID**（生の機微情報・証跡全文は保持しない） | ✅(invoke/feedback) |
| `validated_input` | `dict` | S-2 通過済みの最小化入力（変更理由・変更設計・供給証跡の参照・版情報・mode） | ✅ |
| `version_check` | `dict` | `contract_version` / `authority_matrix_version` / effective date の整合結果（`{ok: bool, mismatches: [...]}`）。不整合は fail-closed の根拠 | ✅(invoke) |
| `source_registry` | `dict` | **決定論的な source ハンドル表**（Stage 1 が供給証跡から採番。`{source_id: {doc_ref, span_ref, span_kind, provenance}}`）。LLM は既存ハンドルの**選択のみ**可・採番不可。`span_kind` は引用可否を決める決定論ラベル（§span kind allowlist） | ✅(invoke) |
| `docs_present` | `dict` | Stage 1 の証跡有無結果 | — |
| `format_check` | `dict` | Stage 1 の書式検証結果 | — |
| `authority_match` | `dict` | Stage 1 の権限表決定論照合結果（決裁者と専決区分の一致・不一致） | — |
| `recalled_perspectives` | `list` | Stage 2 の承認済み事実確認観点（source-linked） | — |
| `evidence_brief` | `dict` | 決裁ブリーフのドラフト（下記出力契約） | — |
| `evidence_map` | `dict` | claim→source 対応マップ（Stage 4 が `source_registry` から構築） | — |
| `gaps` | `list` | 不足論点 | — |
| `requires_human_review` | `bool` | 人間レビュー要否。`True` のとき `evidence_brief` は返さない | — |
| `degradation` | `dict` | **縮退エンベロープ**（`requires_human_review == True` のときのみ設定）。`{review_reason_code: str, gap_codes: [str], unknown_codes: [str]}`。値は下記の閉じた列挙のみ | —(invoke) |
| `ingest_result` | `dict` | `mode=ingest` の蓄積結果（件数・品質フラグ） | —(ingest) |
| `feedback_result` | `dict` | `mode=feedback` の反映結果（承認済み観点・監査 ID） | —(feedback) |
| `result` | `dict` | S-3 通過済みの整形出力（mode 別。invoke=ブリーフ、ingest/feedback=蓄積サマリ） | — |
| `status` | `str` | outer routing 用。`AgentStatus.<X>.value`（`"success"`/`"retry"`/`"error"`）を返す。bare Enum 禁止 | ✅ |

**出力契約（`evidence_brief` — closed allowlist・入れ子まで型で閉じる）:**

トップレベルは 7 フィールド固定: `perspectives`（人間承認済み事実確認観点）/ `stated_facts`（変更
リクエストの記載事実）/ `evidence_sources`（source ID）/ `gaps`（不足論点）/ `contradictions`（矛盾）/
`verification_questions`（決裁者が確認すべき質問）/ `unknowns`（判断材料不足）。

以下に相当するフィールドは **schema に持たない（構造的排除）**:
`clause_permissibility_verdict`（約款許容性）/ `legality_verdict`（適法性）/
`material_change_verdict`（実質的変更）/ `competition_impact_verdict`（競争性）/
`approval_recommendation`（承認推奨）/ `estimate_validity_verdict`（積算・工期の妥当性）。

> 🔴 **キー名だけの allowlist では意味漏洩を止められない**（設計レビュー指摘・High）。
> 「`verification_questions` に『本変更は約款上許容されるため承認して差し支えない』と書く」ような、
> **許可フィールドの自由文に結論を密輸する**経路が残る。したがって allowlist は**入れ子の値まで**
> 閉じる。各要素の型は以下に固定し、**JSON Schema を `additionalProperties: false` で全階層に適用**する。

| フィールド | 要素の型（全階層 closed） | LLM が書ける自由文 |
|---|---|---|
| `perspectives[]` | `{perspective_id, source_id}` — `perspective_id` は ingest 済み承認観点の**既存 ID の選択のみ**（採番不可） | なし |
| `stated_facts[]` | `{fact_id, perspective_id, source_id, span_ref, verbatim_quote}` — `verbatim_quote` は**供給証跡からの逐語引用**で、`source_registry` の span と**完全一致検証**する。加えて参照先 span の **`span_kind` が引用可の種別**でなければ reject（§span kind allowlist） | なし（逐語のみ・要約や言い換えを許さない） |
| `evidence_sources[]` | `{source_id, doc_ref, provenance}` — Stage 1 が採番した registry の射影 | なし |
| `gaps[]` | `{perspective_id, gap_code}` — `gap_code` は config の**閉じた列挙**（`evidence_absent` / `evidence_unreadable` / `version_mismatch` / `authority_unmatched` / `estimate_basis_absent` / `schedule_impact_absent`） | なし |
| `contradictions[]` | `{perspective_id, source_id_a, source_id_b, contradiction_code}` — `contradiction_code` も閉じた列挙 | なし |
| `verification_questions[]` | `{question_template_id, perspective_id, source_id?}` — **人間が事前承認した質問テンプレート ID の選択のみ**。表示文は config のテンプレートから**決定論的にレンダリング**する | なし |
| `unknowns[]` | `{perspective_id, unknown_code}` — 閉じた列挙 | なし |

**この設計の要点**: LLM の役割は「どの観点に、どの証跡の、どの span が対応するか」の**選択と対応付け**で
あって、**事実主張の文字列を書くことではない**。人間が読む散文は、上記の型付きフィールドから
テンプレートで組み立てる。これにより「許可フィールドの自由文に結論が混入する」経路が構造的に消える。

> 部分的な例外として `stated_facts[].verbatim_quote` だけが原文由来の長い文字列を持つが、これは
> **LLM が書いた文ではなく供給証跡からの逐語引用**であり、`source_registry` の span と完全一致しない
> ものは S-3 が reject する（LLM が地の文を混ぜられない）。

> 🔴 **設計レビュー指摘（High）— 逐語引用でも結論は出せる**: span 完全一致は**出所を保証するだけで、
> 内容が非結論的であることは保証しない**。供給証跡（例: 過去の決裁文書・顧問弁護士の意見書・
> 監督官庁への照会回答）に「本変更は約款上許容される」と書かれていれば、それを逐語引用するだけで
> **禁止された法的結論が出力に載る**。「LLM に書かせない」だけでは閉じない経路である。

**対策 — 引用可能な span を種別で制限する（span kind allowlist）**

`source_registry` の各 span には Stage 1 が**決定論的に `span_kind` を付与**し、
`verbatim_quote` として出力できるのは**事実記述系の種別に限る**:

| `span_kind` | 引用可否 | 例 |
|---|---|---|
| `quantity` | ✅ | 「土工増 ¥3,200,000」「工期 30 日延長」 |
| `date` | ✅ | 「令和8年5月12日 協議」 |
| `identifier` | ✅ | 「工事番号 R8-道改-014」 |
| `observed_condition` | ✅ | 「掘削中に設計図書に記載のない埋設管を発見」 |
| `procedural_record` | ✅ | 「管理者協議を実施」 |
| `adjudicative` | ❌ **引用不可** | 「約款上許容される」「変更は適法」「承認が相当」 |
| `opinion` | ❌ **引用不可** | 意見書・法的評価・妥当性判断の記述 |
| `unclassified` | ❌ **引用不可**（fail-closed の既定） | 分類できなかった span |

- **既定は `unclassified` = 引用不可**（分類に失敗したものを通さない fail-closed）。
- S-3 は `verbatim_quote` を持つ全 `stated_facts` について、参照先 span の `span_kind` が
  引用可の列挙に属することを検証し、`adjudicative` / `opinion` / `unclassified` を **reject** する。
- 引用不可の span しか根拠が無い観点は、引用を出さず `gaps` / `unknowns` に落とす
  （「根拠はあるが引用できない」ことを人間に示す）。
- `span_kind` の付与は Stage 1 の決定論処理（原文の構造・見出し・定型表現による分類 + config の
  分類ルール）で行い、**LLM に分類させない**（LLM に分類させると `adjudicative` を `observed_condition` と
  誤ラベルして通す経路ができる）。分類の網羅性は原理的に完全でないため、`unclassified` の
  fail-closed が最後の砦になる。

> この対策により、「LLM が書かない」＋「引用元の種別も制限する」の二段で、
> 出力に法的結論が載る経路を閉じる。MR-B の受入テストでは、`adjudicative` span を含む証跡を
> 入力して**引用されずに `gaps` へ落ちること**を検証する。

**State Constraints (mandatory):**
- Flat TypedDict のみ（primitives + JSON-serializable）
- JWT / API keys / credentials を State に置かない（checkpoint DB 漏洩）。生の機微情報・証跡全文も置かない（参照 ID のみ）
- InvocationContext は `config["configurable"]` 経由のみ（State に置かない・`InvocationContext.from_state(state)` で取得）
- Pydantic / dataclass / 任意 Python オブジェクト禁止（msgpack 非互換）

## Framework Utilization

### Shared Components Used

- [x] InvocationContext（correlation_id, session_id, permissions, credential handle）— ノード内では `InvocationContext.from_state(state)` で取得（直接構築しない）。State には**格納しない**
- [x] `ctx.secrets.require("KEY")` で secrets 取得（`os.environ` 禁止・入口 caller-auth token を除く）
- [x] SecurityViolationError（S-2/S-3 違反時）
- [x] **S-2**: `PreProcessNode._extra_security_gate_input()` — **raw `user_input` + invocation config のみを読む**（`execute()` が書く `mode` / `case_ref` に依存しない＝ゲートは execute の前に走るため）。チェック: 機微情報最小化・入力サイズ制限・mode allowlist（raw の mode 値）・**IMP-76 規制データ検知（GOV は対象業種。My Number 識別子・行政文書の機密区分マーカー・APPI 17 条 要配慮個人情報）**。顧客スコープ解決の fail-closed は **`execute()` 側**で行い、解決不可なら `status="error"` を返す。既定 PII スキャン（`detect_pii`）の後に走る。**`_security_gate_input()` は override しない**（`@final`）
- [x] **S-3**: `PostProcessNode._extra_security_gate_output()` — **allowlist 主軸**（後述）。既定 credential スキャン（`detect_credentials`）の後に走る。**`_security_gate_output()` は override しない**（`@final`）
- [x] **S-4**: 各 `execute()` 内で `emit_trace_event()`（`shared.utils.audit_logger`）を最低 1 回（`node_start` / `node_complete` / `node_error` は emit しない）
- [x] **substrate 再利用**: credential/PII の正規表現を自前実装しない（`framework.security.detect_credentials` / `detect_pii` が `FunctionNode` により自動実行される）。LLM 呼び出しは `config["llm"]` 注入シーム（`BaseLLM`）で受け、具体クライアント・モデル ID をテンプレートに書かない
- [x] **`ContractVariationGraphNode`（GraphNode）の S-2/S-3**: 内側 graph / 子ノードでゲート適用済みのため、outer の GraphNode 側は deliberate no-op passthrough（ADR-017）

> **S-2/S-3 gate behaviour by node type (ADR-017):**
> - `FunctionNode` subclass（PreProcessNode / PostProcessNode / 内側 Stage 各ノード）→ framework `@final` gate が自動実行。`_extra_*` フックで拡張のみ
> - `GraphNode`（ContractVariationGraphNode）→ deliberate no-op（内側ノードのゲートが適用済み）

### S-3 設計方針 — allowlist 一本化（denylist を持たない）

> **既定方針に従い、手書きの断定語 denylist は実装しない。**
> 禁止フレーズの正規表現は言い換えで原理的にすり抜け（「約款上問題ないので変更できます」等）、
> 守っているつもりで守らない false assurance になるため。非結論性は**構造**で担保する。

`_extra_security_gate_output()` が検査するのは以下の 4 点のみ:

1. **出力構造の closed allowlist** — `evidence_brief` を**全階層** `additionalProperties: false` の
   JSON Schema で検証し、宣言外キーを**再帰的に reject**。上記「構造排除」対象の結論フィールド名は
   allowlist に存在しないため、生成されても構造的に落ちる。**値の型も上表のとおり閉じる**（ID・
   閉じた列挙・逐語引用のみ。自由文フィールドを持たない）
2. **claim→source 実在検証** — 全 `stated_facts` が `source_registry` に実在する `source_id` を参照し、
   `verbatim_quote` が該当 span と**完全一致**すること（捏造 ID・link 欠落・引用改変は reject）。
   `evidence_map` は registry から決定論的に構築されたものと一致すること。
   `perspective_id` / `question_template_id` / 各 `*_code` も**既存の承認済み ID・列挙値に実在照合**する
3. **観点カバレッジ** — `recalled_perspectives` の各観点が `perspectives` で被覆されていること（欠落は `gaps` へ）
4. **PII / credential 残存 block** — framework 既定スキャンの後段で、出力 dict を直接走査して残存を検知

#### 出口の一元化（egress choke point）

> 🔴 **設計レビュー指摘（High）**: S-3 は outer `post_process` でしか走らないため、
> **①検証失敗時の縮退返却 ②内側エラーの伝播（`SubgraphError`）**
> は S-3 を通らずに外へ出る経路になる。ここに Stage 3 の未検証ドラフトや例外文が乗ると、
> 構造排除を宣言しているのに結論・機微が漏れる。

対策として、**`src/services/egress.py` に単一のサニタイザ `sanitize_for_egress(payload, kind)` を置き、
外に出る全経路がこれを必ず通る**設計にする:

| 経路 | 通す内容 |
|---|---|
| `post_process` の `result` | S-3 検証済みの `evidence_brief`（上表の型付きフィールドのみ） |
| 検証失敗時の縮退返却 | `evidence_brief` を**返さない**。`{requires_human_review: true, review_reason_code, gap_codes, unknown_codes}` の型付きコードのみ（ドラフト本文を載せない） |
| エラー envelope | `{status:"error", error_code}` のみ。**例外文字列・スタックトレース・入力エコーを載せない** |

`requires_human_review == True` のときは `evidence_brief` を返さない（検証を通らなかったドラフトが
「参考情報」として出るのを防ぐ）。

**縮退コードの閉じた列挙（config 定数・自由文を許さない）**

> 🔴 **設計レビュー指摘（High）**: 「型付きコード」と書いても列挙が定義されていなければ、
> `review_reason_code` に「約款上許容されるため承認可」という**自由文を入れられる**。
> 縮退経路そのものが結論の抜け道になる。

| フィールド | 許可値（これ以外は `sanitize_for_egress` が reject） |
|---|---|
| `review_reason_code` | `version_mismatch` / `citation_missing` / `citation_span_mismatch` / `confidence_below_threshold` / `authority_unmatched` / `required_evidence_absent` |
| `gap_codes[]` | `evidence_absent` / `evidence_unreadable` / `version_mismatch` / `authority_unmatched` / `estimate_basis_absent` / `schedule_impact_absent`（§出力契約 `gaps` と同一列挙） |
| `unknown_codes[]` | `perspective_not_recallable` / `source_unresolvable` / `version_undeclared` |

値は Stage 1 / Stage 4 の**決定論的な検証結果から導出**する（LLM に選ばせない）。
MR-B の受入テストで、各縮退フィールドに法的結論の文字列を入れた negative test を必ず書く。

**エラー境界の一元化（`SubgraphError` の catch/wrap 地点）**

> 🔴 **設計レビュー指摘（High）**: `error_strategy: propagate` により内側エラーは `SubgraphError` として
> 再送出されるが、それを**どこで捕まえて `{status, error_code}` に変換するか**が未指定だった。
> 現在の scaffold の `src/api/server.py` は `agent.invoke(...)` の戻りをそのまま返すため、
> 例外が envelope を素通りする。

`src/api/server.py` の**アプリケーション境界に単一のハンドラ**を置き、グラフ／ドメイン例外を捕捉して
承認済み `error_code` にマップし、`sanitize_for_egress(payload, kind="error")` を通してから返す。
`error_code` も閉じた列挙（`scope_unresolved` / `input_rejected` / `upstream_failure` /
`internal_error`）とし、**例外メッセージ・スタックトレース・state・入力エコー・ドラフトを載せない**。
MR-B でこれらが応答に現れないことをテストする。

#### S-4 監査ログの payload allowlist

> 🔴 **設計レビュー指摘（High）**: ログも egress チャネルである。`validated_input`・LLM ドラフト・
> 証跡 span・feedback 本文・例外文をそのまま `emit_trace_event()` に載せると、出力を閉じても漏れる。

`emit_trace_event()` に渡す payload は **ID・件数・コード・ステータスのみ**に限定する
（`case_ref` / `perspective_id` / `source_id` / 各 `*_code` / 件数 / `status` / 経過時間）。
**原文・ドラフト・引用文・feedback 本文・例外メッセージは載せない**。これを MR-B の受入テストで
「ログを egress として検査する」形で検証する。

> S-3 は**製品安全の緩和策**であって法令遵守を確立するものではない。基準検証は顧客（自治体）法務・
> 監査部門の責務である。

## 人間関与の設計（HITL 非採用）

**`hitl.enabled` は設定しない**（`config/config.yaml` は `memory_enabled: false`）。
グラフ内の `interrupt()` による suspend/resume は行わない。**PB-7 は auto-waived（非 HITL）**。

### 根拠（ユーザー判断・2026-07-28）

人間の判断は本 AG に不可欠だが、それを**グラフ内 suspend で実現する必然性がない**:

1. **AG は決裁判定をしない** — 最終判定は決裁権者が AG の外で行う。AG 実行中に人間の承認を挟んで
   処理が続く構造ではない（そもそも承認結論を出力しない設計＝§出力契約）。
2. **検証失敗は fail-closed で正常終了** — 版不整合・引用の非実在・低信頼のいずれでも
   `evidence_brief` を返さず、`requires_human_review: true` + `gap_codes` / `unknown_codes` /
   `review_reason_code` を返して終了する。セッションを保持する理由がない。
3. **ターン間隔が業務実態と合わない** — 「工程影響の根拠が足りない」と示された担当者は、その場では
   直せない（工事事務所への照会・資料取り寄せに数時間〜数日）。数分のターン間隔を前提とする
   D6 `interrupt()` は不適合で、再呼び出し方式が正しい（方式選択基準・PCRepairIntakeTriageAgent 判例）。
4. **人間関与は `/feedback` で担保** — 人間が確定した事実確認観点は `mode=feedback` で明示承認 +
   監査イベント付きで蓄積され、次回以降の `/invoke` に効く（非同期の人間関与）。

同型の前例 = Logistics Dangerous-Goods Document Consistency Agent（HITL 非採用・`/feedback` で代替・PB-7 auto-waived）。

### 再呼び出しのループ（正常系）

```
/invoke → 検証失敗 → {requires_human_review: true, review_reason_code, gap_codes}
   ↑                                   │
   └── 担当者が不足証跡を補って再実行 ←─┘
```

`requires_human_review == True` のとき `evidence_brief` は**返さない**（検証を通らなかったドラフトが
「参考情報」として出るのを防ぐ）。返すのは型付きの不足コードのみで、担当者は何を補えばよいか分かる。

### GraphNode の subgraph 保持

`get_subgraph()` は `__init__` で 1 度だけ構築・`compile()` した内側グラフを返す
（毎回新規生成しない）。HITL を使わないため resume 経路は通らないが、**呼び出しごとにグラフを
再構築・再 compile するのは単純に無駄**であり、姉妹テンプレート（Financial Regulatory Consultation Response Evidence Brief Agent `ConsultationBriefGraphNode`）に
揃える。保持するのは構築時に確定し以後書き換えない**不変のグラフ識別**であって、
禁止対象の「実行ごとに書き換わる業務データ（`self._result` / `self._cache`）」には当たらない。

> **STG 運用の見込み**: 非 HITL のため `deploy-stg` の `agent_status_success` は
> **構造的 FAIL にならない**（HITL AG のような `awaiting_human` 留置は発生しない）。
> ただし本 AG は顧客専用接続を要する fail-closed 型のため、STG スモークが正規入力を持たず
> `status=error` になる可能性がある。その場合は `stage:stg-complete` を付けず `stage:stg` に留め、
> 構造的理由を `work/03_review_guide.md` と `work/OPS_TRACKER.md` に記録する
> （fail-closed AG の STG 挙動・既知パターン）。**STG を通すための seed 経路を本番コードに入れない。**

### Composition Pattern

- **Pattern**: **GraphNode (subgraph)** — main スロットに `ContractVariationGraphNode` を割り当て、
  内側 `ContractVariationWorkflowGraph`（`src/graph/domain_workflow_graph.py`）を呼ぶ
- **Composition target**: `ContractVariationWorkflowGraph`（inner）
- **GraphNode 契約**:
  - `get_subgraph()` → 内側 graph を返す（インスタンスをキャッシュ）
  - `extract_input()` → **最小マップを明示的に渡す**: `{mode, case_ref, validated_input}` を JSON 直列化
    （`correlation_id` / `session_id` / `caller_trust_level` は framework が `GraphNode` 経由で自動伝播）
  - `merge_output()` → **mode 別に明示マッピング**（raw pass-through しない）:
    - `invoke`: `evidence_brief` / `evidence_map` / `gaps` / `requires_human_review` / **`degradation`** / `version_check` / `status`
      （`requires_human_review == True` のときは `evidence_brief` / `evidence_map` を**マップしない**＝親 state に載せない）
    - `ingest`: `ingest_result` / `status`
    - `feedback`: `feedback_result` / `status`
- **Error propagation strategy**: `propagate`（内側エラーは `SubgraphError` として再送出・fail fast）
- **propagate_hitl**: `false`（既定のまま。本テンプレートは HITL を使わない）

> **post_process の分岐契約**（順序が重要）:
> 1. 上流 `status == "error"` → **そのままパススルー**（下記の検査をしない）
> 2. `requires_human_review == True` → **縮退エンベロープのみ**を検査して `result` に整形し、
>    `status` は `success`（正常終了。`evidence_brief` が無いことを missing-field エラーにしない）
> 3. それ以外の `invoke` → `evidence_brief` / `evidence_map` を S-3 検査して `result` へ整形
> 4. `ingest` / `feedback` → `ingest_result` / `feedback_result` を要約して `result` へ整形
>
> **2 を 3 より先に評価する**こと。逆順だと、ドラフト不在を欠落エラーとして扱う実装や、
> 検証を通っていないドラフトを「参考情報」として返す実装を誘発する（設計レビュー指摘 High）。

## データ統治（永続化）

- **顧客スコープは接続レベル分離**: 自治体ごとに専用 DB / KB 接続を provision する。
  caller 認証 → 認可された顧客 → provision 済み接続、の順で解決し、**入力の `customer_id` は照合のみに使う**
  （それでスコープを決めない）。default KB は持たない・未解決は fail-closed。
- **DB（原記録）**: `baseline_contracts` / `clause_refs` / `authority_matrix` / `review_perspectives` /
  `feedback_audit_events`
- 保持期間・削除は顧客（自治体）の文書管理規程に従う。

### KB 検索セーフ射影（retrieval-safe projection）— 型で閉じる

> 🔴 **設計レビュー指摘（High）**: 「検索セーフ射影」を散文で書くだけでは閉じない。
> 過去の決裁記録や人間 feedback の**自由文**に結論（「前回は許容と判断」）が含まれていると、
> ingest → KB → Stage 2 → **LLM の context** という経路で結論が再流入する（リスク #2 の実体）。

KB に射影するレコードは以下の**閉じた型のみ**とし、自由文フィールドを持たない:

```
perspective_record = {
  perspective_id:        str,   # 承認済み観点の ID（採番は ingest 時の決定論処理）
  perspective_code:      str,   # 閉じた列挙（確認すべき事実カテゴリ。例 change_reason_basis /
                                #   estimate_basis / schedule_impact_basis / authority_conformance）
  required_fact_codes:   [str], # その観点で確認すべき事実の種別（閉じた列挙）
  question_template_ids: [str], # 対応する承認済み質問テンプレート ID
  source_id:             str,   # 根拠の source ハンドル
  provenance:            {doc_ref, contract_version, authority_matrix_version, effective_date},
  approved_by_role:      str,   # 承認者の役割（InvocationContext から解決・リクエスト値は使わない）
  approved_at:           str,
  record_version:        int,
}
```

- **原文・決裁結果・判断理由・feedback 本文は KB へ射影しない**（DB の原記録側にのみ残す）。
  Stage 2 が LLM に渡せるのは上記の型付きレコードだけである。
- **承認者の権限は `InvocationContext` から解決**する。リクエストボディの「承認済みです」という
  自己申告フィールドで承認扱いにしない（H-4 指摘）。
- `mode=feedback` で受けた人間の自由文は**隔離（quarantine）**し、DB の `feedback_audit_events` に
  原記録として保存する。KB へ入るのは、その feedback から**人間が明示的に確定した型付き観点レコード**
  （上記スキーマに適合するもの）だけ。自由文がそのまま検索対象になる経路を作らない。

## 入力形式

- 受け付けるのは軽量テキスト形式（JSON / CSV / プレーンテキスト）に絞る。
- **PDF / Excel（xlsx）は受け付けない**（マジックバイト判定で reject）。パーサ増・OCR 崩れ・依存膨張を避ける。
- 生の証跡全文は State / checkpoint に載せず、参照 ID（`case_ref` / `source_id`）で扱う。

## 適用部品一覧（parts library）

> MR-B で `apply_part.py` を流す際の指示書。骨格を再発明しない。バージョンは `parts/*/PART.yaml` 実体。

| part-id | version | 適用先 | adaptation_points の埋め方 |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.2 | `src/nodes/main_node.py`（`ContractVariationGraphNode`） | cache subgraph・extract/merge skeleton・upstream error passthrough（**HITL 関連の adaptation point〔inner checkpointer 分離・`propagate_hitl`〕は本テンプレートでは未使用**） |
| `output-envelope` | 0.1.2 | `src/nodes/post_process_node.py` | mode / gate_kind 宣言・degradation folding・mode スコープ返却 |
| `evidence-gate` | 0.1.0 | Stage 4 `EvidenceVerifyNode` + `PostProcessNode._extra_security_gate_output()` | 引用 ID の `source_registry` 実在照合・捏造 ID 検出・不一致時 fail-closed |
| `llm-draft-stage` | 0.1.0 | Stage 3 `MappingNode` | プロンプト組立シーム・`config["llm"]` 注入・deterministic fallback・`degradation_reason`・evidence hook |
| `ingest-row-pipeline` | 0.2.0 | `IngestNode` | 品質ゲート→サニタイズ→正規化→書込→`ingest_summary`(written_ids / rejected[]) |
| `ingest-file-sanitizer` | 0.1.0 | `PreProcessNode` / `IngestNode` | マジックバイト判定・PDF/Excel を含む未知形式 reject・索引テキスト品質ゲート |
| `entry-adapter` | 0.3.0 | `src/api/server.py` | verified trust gate・verified-state scope seam・client header scope 禁止。**`/resume` エンドポイントは実装しない**（HITL 非採用のため suspend 中セッションが存在しない）＝部品の `/resume` 認証 adaptation point は未使用。`/invoke` `/ingest` `/feedback` の 3 経路すべてに同一の入口認証を適用する |

**不採用の部品と理由（MR-B で再確認する）:**

| part-id | 不採用の理由 |
|---|---|
| `scoped-retrieval` | scope キーは「1 顧客 KB 内で混ぜてはいけない区画を絞る行キー」であり、**マルチテナント分離用ではない**（USAGE.md 冒頭警告）。本 AG は顧客専用接続による物理分離で、1 自治体の KB 内にさらに分ける区画が無いため入れない。Stage 2 の検索は顧客専用接続に対する top_k + safe projection を自前で実装する |
| `payload-store` | 生の証跡本文をグラフ内 state に載せない設計（参照 ID のみ）のため、checkpoint に隔離すべき機微本文が無い |
| `feedback-mini` | registry 上まだ `ready` でない（second_wave・実体未抽出）。`FeedbackNode` は自前実装し、部品化は将来の横展開時に検討 |

## config/agent.yaml・config/config.yaml の設計値

`config/agent.yaml`（静的マニフェスト）:

```yaml
agent:
  id: "GOV-C2-110"
  name: "MunicipalContractVariationEvidenceBriefAgent"
  category: "Cat 2"
  industry: "GOV"
  base_type: "AgentBaseGraph"          # L1 クラス名（DocGenerationAgent はパターン名なので不採用）
  module: "src.graph"
  class: "ContractVariationEvidenceBriefAgent"
  required_trust_level: "VERIFIED_EXTERNAL"
  requires:
    secrets:
      - "CONTRACT_VARIATION_DB_DSN"
      - "CONTRACT_VARIATION_KB_ENDPOINT"
      - "CONTRACT_VARIATION_LLM_API_KEY"
```

`config/config.yaml`（ランタイム・本 MR で作成済み）: `max_retry: 3` / `timeout_s: 60` /
`memory_enabled: false`（HITL 非採用のため checkpointer 不要） / `top_k: 8`（Stage 2 想起件数）/
`evidence_confidence_threshold: 0.75`。数値は**テンプレート利用者が自環境で調整する前提の初期値**で、
STG 実測後に調整する。

> 🔴 **MR-B の完了条件（manifest 解決・設計レビュー指摘 High）**: `agent.yaml` は
> `module: src.graph` / `class: ContractVariationEvidenceBriefAgent` を宣言しているが、
> 現在の `src/graph/graph.py` は scaffold 雛形の `class Graph` のままである。MR-B で
> **①クラスを `ContractVariationEvidenceBriefAgent` に改称 ②`src/graph/__init__.py` で re-export**
> の両方を行う（片方だけでは AgentRegistry が解決できない。Return Fraud Investigation Memo Drafting Agent でレビュー High として実際に出た同型）。

## S-1 Trust Level

本テンプレートは自治体の内部業務データを読み書きするため、**全ノードで
`required_trust_level = TrustLevel.VERIFIED_EXTERNAL`** をクラス本体に明示宣言する
（暗黙の `ANONYMOUS` 継承は不可。有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 値のみ）。

対象は `FunctionNode` サブクラスだけではない:

| ノード | 宣言 |
|---|---|
| `PreProcessNode` / `PostProcessNode`（FunctionNode） | `VERIFIED_EXTERNAL` |
| **`ContractVariationGraphNode`（GraphNode = 直接の `BaseNode` サブクラス）** | `VERIFIED_EXTERNAL` |
| 内側 Stage 1〜4 / Ingest / Feedback 各ノード | `VERIFIED_EXTERNAL` |

> 🔴 **設計レビュー指摘（Medium）**: `GraphNode` は `FunctionNode` ではなく直接の `BaseNode` サブクラス
> であり、宣言を書かないと**既定の `ANONYMOUS` を継承**して S-1 ゲートが素通りする。
> `agent.yaml` の `required_trust_level` は AgentRegistry 用のマニフェスト値であって
> **ノードの S-1 ゲートを代替しない**。MR-B では GraphNode を trust 不足で直接呼ぶテストを
> TC-08 の一部として書く。

## Import Isolation Confirmation

- [x] Template does not import agenticstar-platform SDK (Level 0)
- [x] Import targets: framework/ and shared/ only (no agents/base/ required)

## 設計着手条件の反映（レビュー所見 Medium）

| # | 受入条件 | 反映先 |
|---|---|---|
| 1 | 入力に `contract_version` / `authority_matrix_version` / effective date を必須化 | §State Definition（`validated_input` / `version_check`）/ Stage 1 `DeterministicCheckNode` |
| 2 | 不一致・欠落時は条項解釈・承認結論をせず `unknowns` / `questions` に fail-closed | Stage 1（意味的照合へ進まない）/ §出力契約（`unknowns` / `verification_questions`）/ Stage 4 の fail-closed 条件 |

受入テスト（版不整合で結論を出さないこと・欠落で fail-closed すること）は `docs/03_test_spec.md`（MR-B）へ落とす。

## SDK 実機確認（AgentCore SDK 1.0.0・2026-07-27）

design が依拠するシンボルを venv（`agenticstar-agentcore==1.0.0`）で introspection し実在を確認した:

| 確認対象 | 結果 |
|---|---|
| `TrustLevel` の有効値 | `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 値（文字列 enum・int 比較不可） |
| `AgentStatus` の値 | `pending` / `success` / `retry` / `error` / `timeout` / `awaiting_human` / `cancelled` |
| `AgentBaseGraph` の abstract | `name` のみ（`register_nodes` / `add_edges` は実装済み＝override 不要） |
| `GraphNode` の abstract | `get_subgraph` / `extract_input` / `merge_output` の 3 つ |
| `GraphNode` の class var 既定 | `error_strategy="propagate"` / `propagate_hitl=False`（本設計は両方とも既定のまま） |
| `FunctionNode` の abstract | `execute` のみ（`_security_gate_input/output` は `@final` 実装済み） |
| `AgentState` のフィールド | `hitl_allowed` / `hitl_draft` / `hitl_status` / `hitl_count` / `subgraph_thread_id` / `validated_input` / `result` / `formatted_output` / `status` / `node_history` 等を確認 |

## ユーザビリティ設計レビュー（修正した点）

エンドユーザー（自治体 契約管理担当・決裁権者）の利用実態を想定して設計を見直し、以下を修正した:

1. **入力の過剰構造化を回避** — 当初は変更理由を観点別に分割入力させる案だったが、担当者が起案時に
   持っているのは変更理由・変更設計の**自由記述**（起案文書そのもの）である。自由記述をそのまま受け取り、
   観点への割り付けは AG 側（Stage 3）で行う設計に変更した。これが本 AG の価値の核でもある。
2. **出力を次アクションに直結する形へ** — 「不足あり」だけでは起案者が動けないため、`gaps` と
   `verification_questions` を分離し、**決裁者が確認すべき質問**の形で出す。起案者はこれを埋めてから
   決裁に上げられる。
3. **版不整合時の縮退を「無言の低品質出力」でなく明示停止に** — 版が合わない状態で意味的照合をすると
   もっともらしいが根拠の無いブリーフが出る。`unknowns` / `verification_questions` へ fail-closed し、
   担当者に不足コードを返して再実行してもらう設計にした（レビュー所見と同趣旨）。
4. **往復を「再呼び出し」で設計** — 決裁準備は一発で完成しない。ただし不足を指摘された担当者が
   証跡を揃え直すには数時間〜数日かかるため、セッションを suspend して待つ HITL ではなく、
   fail-closed で終了して**担当者の都合で再 `/invoke` する**方式にした（ユーザー判断 2026-07-28）。

## 設計レビュー（push 前・敵対レビュー）の指摘と対応

| # | Sev | 指摘 | 対応 |
|---|---|---|---|
| 1 | High | `config/config.yaml` が存在せず、ランタイム値が静的マニフェストに入っていた。`timeout` も 30 と 60 で不整合 | **修正**: `config/config.yaml` を新設し `agent.yaml` からランタイム値を除去（静的マニフェストとランタイム設定の分離）。<br>**追記（2026-07-28 ユーザー判断で HITL 非採用に転換）**: 当初は本指摘を受けて `hitl.enabled: true` を config.yaml に置き PB-7 を実効化したが、その後の設計判断で **HITL 自体を不採用**とした（§人間関与の設計）。したがって `memory_enabled: false`・`hitl.enabled` なし・**PB-7 は auto-waived（非 HITL）**が正となり、PB-7 スタブは scaffold 原本のまま（拡張を巻き戻し済）。本指摘の本質（ランタイム値の置き場所）は解消済み |
| 2 | High | closed allowlist が**キー名だけ**を閉じており、許可フィールドの自由文（特に `verification_questions`）に結論を密輸できる | **修正**: 出力契約を**入れ子の値まで型で閉じる**設計に改訂（ID・閉じた列挙・逐語引用のみ／LLM が書ける自由文フィールドをゼロにする）。人間が読む散文は型付きフィールドから決定論レンダリング |
| 3 | High | S-3 は outer `post_process` でしか走らず、**検証失敗時の縮退返却・エラー envelope** が S-3 を迂回する | **修正**: `src/services/egress.py` の単一サニタイザに全出口を集約。`requires_human_review` 時は `evidence_brief` を返さず型付きコードのみ・エラーは `error_code` のみ（例外文を出さない）。S-4 ログの payload allowlist も明記。（当初は HITL interrupt payload も対象に含めたが、HITL 非採用によりその経路自体が消滅） |
| 4 | High | KB の「検索セーフ射影」が散文で、過去決裁・feedback の自由文経由で結論が LLM context に再流入し得る | **修正**: KB 射影レコードを**閉じた型**で定義（自由文フィールドなし）。承認者の権限は `InvocationContext` から解決（リクエストの自己申告を使わない）。feedback 自由文は DB へ隔離し KB へ入れない |
| 5 | High | `get_subgraph()` のインスタンスキャッシュは「node インスタンス変数に可変状態を持つな」の禁止に抵触する | **棄却**: 禁止対象は実行ごとに書き換わる業務データ（`self._result` / `self._cache` 等）であり、構築時に確定し以後書き換えない**不変のグラフ識別**（compile 済みオブジェクト）は該当しない。リクエスト間で共有されても競合しない。マージ済み前例 Financial Regulatory Consultation Response Evidence Brief Agent も同型。<br>**追記（2026-07-28）**: 当初の棄却根拠は「resume 経路で checkpointer 識別が変わる」だったが、HITL 非採用により resume 経路自体が消滅したため、その論拠は失効。現在の棄却根拠は上記の**可変業務データではない**という点のみ（キャッシュは正しさの要件ではなく、毎回の再構築・再 compile を避ける効率上の選択） |
| 6 | High | `agent.yaml` が `ContractVariationEvidenceBriefAgent` を指すが `src/graph` は `class Graph` のままで AgentRegistry が解決できない | **MR-B の完了条件として明記**（改称 + `__init__` re-export の両方）。`src/` の実装は MR-B スコープのため本 MR では実装しない |
| 7 | Medium | `GraphNode` は `FunctionNode` でないため、trust 宣言を書かないと既定 `ANONYMOUS` を継承し S-1 が素通り | **修正**: §S-1 に「GraphNode を含む全ノードで `VERIFIED_EXTERNAL` を明示宣言」を表で明記。MR-B で trust 不足の直接呼び出しテストを TC-08 に含める |
| 8 | Medium | 内側グラフの基底クラスと実装契約が未指定（abstract 未実装で instantiate 失敗の恐れ） | **修正**: `ContractVariationWorkflowGraph(BaseGraph)` と `BaseGraph` の abstract 7 つ（`name`/`state_schema`/`_validate_config`/`register_nodes`/`add_edges`/`route`/`get_output`）を実機確認して明記。compile 済みで返す点も明記 |
| 9 | Medium | S-4 監査ログの payload allowlist が未定義でログが漏洩チャネルになる | **修正**: §S-4 監査ログの payload allowlist を新設（ID・件数・コード・status のみ／原文・ドラフト・引用・feedback 本文・例外文は載せない）。MR-B でログを egress として検査するテストを書く |

### 第 2 ラウンド（HITL 非採用への転換後・2026-07-28）

HITL 削除の整合性と、削除で新たに開く穴がないかを焦点に再レビューした。

| # | Sev | 指摘 | 対応 |
|---|---|---|---|
| 10 | High | 縮退エンベロープのフィールド（`review_reason_code` / `gap_codes` / `unknown_codes`）が **State・内側 `get_output`・`merge_output` のどこにも存在しない**。一方 `post_process` は invoke で必ず `evidence_brief` を検査すると書いてあり、ドラフト不在の縮退経路と矛盾する（missing-field エラー化 or 未検証ドラフト温存を誘発） | **修正**: State に `degradation` を first-class フィールドとして追加し、内側 `get_output` → `merge_output`（invoke）→ `post_process` まで一貫して通した。`post_process` の分岐契約を**順序付きで明記**（①上流 error パススルー → ②`requires_human_review` なら縮退エンベロープのみ検査して `success` → ③通常 invoke → ④ingest/feedback）。`requires_human_review` 時は `evidence_brief`/`evidence_map` を親 state にマップしない |
| 11 | High | 「型付きコード」と書いてあるだけで**列挙が未定義**。`review_reason_code` に自由文を入れれば縮退経路そのものが結論の抜け道になる | **修正**: 3 フィールドすべてに**閉じた列挙**を定義（§縮退コードの閉じた列挙）。値は Stage 1 / Stage 4 の決定論的検証結果から導出し LLM に選ばせない。`sanitize_for_egress` が列挙外を reject。MR-B で各縮退フィールドに法的結論文字列を入れる negative test を必須化 |
| 12 | High | `error_strategy: propagate` で `SubgraphError` が再送出されるのに、**catch/wrap 地点が未指定**。現 scaffold の `src/api/server.py` は `agent.invoke(...)` をそのまま返すため例外が envelope を素通りする | **修正**: `src/api/server.py` のアプリケーション境界に**単一のエラーハンドラ**を置き、承認済み `error_code`（閉じた列挙）へマップして `sanitize_for_egress(kind="error")` を通す設計を明記。例外文・スタックトレース・state・入力エコー・ドラフトを載せないことを MR-B でテスト |
| 13 | High | **逐語引用は出所を保証するだけで非結論性を保証しない**。供給証跡（過去決裁・意見書等）に「本変更は約款上許容される」とあれば、逐語引用するだけで禁止結論が出力に載る。「LLM に書かせない」だけでは閉じない | **修正**: `source_registry` の各 span に Stage 1 が**決定論的に `span_kind` を付与**し、引用可能な種別を allowlist で制限（§span kind allowlist）。`adjudicative` / `opinion` / **`unclassified`（既定・fail-closed）**は引用不可。引用不可 span しか根拠が無い観点は `gaps` / `unknowns` へ落とす。分類は LLM にさせない。MR-B で `adjudicative` span が引用されず `gaps` に落ちることを検証 |
| 14 | Medium | PB-7 スタブが `config/agent.yaml` しか見ないが、ランタイム `hitl.*` の正しい置き場所は `config/config.yaml`。**現状は両方に HITL が無いため正しく skip する**が、将来 config.yaml で HITL を有効化すると PB-7 が黙って waive される | **本テンプレートでは実害なし**（HITL 非採用のため）。ただし**テンプレート共通の scaffold 側課題**であり、テンプレ側で `_CONFIG_PATH` を書き換えると scaffold 自己回帰テストを壊す（Financial Regulatory Consultation Response Evidence Brief Agent で実際に High として出た同型）。→ **共通側への改善要望として記録**（起票自体は人間の判断） |
| 15 | Medium | `entry-adapter` 部品の適用指示に `/resume` の認証・checkpoint 所有権検証が残っており、HITL 非採用と矛盾 | **修正済**（レビュー前に自己検出して修正）: `/resume` は**実装しない**と明記し、部品の該当 adaptation point を未使用とした。`/invoke` `/ingest` `/feedback` の 3 経路に同一の入口認証を適用 |

**確認できた点（指摘なし）**: `HITL Required: No` は**許容**（GOV Cat 2 に求められるのは「HITL Gate を判断して埋めること」であって
HITL の有効化ではない。HITL 関連の契約は `hitl.enabled: true` のときのみ適用される）。
`memory_enabled: false` / `hitl` キーなし / `propagate_hitl: false` / checkpointer なしは相互に整合。
`requires_human_review` は「人間が証跡を補正して**新しい `/invoke` を投げる**」意味として有効で、
`/feedback` は resume ではなく非同期の統治更新として別物。

> **MR-B への申し送り（scaffold 実装の置換）**: 現在の `src/nodes/main_node.py` は scaffold 雛形のままで
> `validated_input` をそのままエコーする。これは**利用者が入力した法的結論をそのまま返す**経路であり、
> MR-B で本設計のワークフローに**完全に置き換える**まで deploy してはならない。
> 本 MR-A は docs/config のみの変更で `src/` を触っていないため、この置換は MR-B のスコープ。

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | **AgentBaseGraph** | AutonomousBaseGraph | AgentBaseGraph | 決定論的な pre→main→post パイプライン。自律 think-act ループは不要（Cat 2） |
| Composition pattern | Standalone | **GraphNode (subgraph)** | GraphNode | mode 分岐 + Stage 1〜4 の多段ドメインロジックを内側 graph に隠蔽 |
| Error propagation | **propagate** | handle | propagate | 根拠検証は fail fast。graceful degradation は安全側でない |
| HITL | **無効（再呼び出し方式）** | 有効（D6 interrupt） | 無効 | AG は決裁判定をせず、検証失敗は fail-closed で正常終了する。証跡を揃え直すターン間隔が数時間〜数日で D6 は不適合（PCRepairIntakeTriageAgent 判例）。人間関与は `/feedback` で担保。PB-7 auto-waived（ユーザー判断 2026-07-28） |
| 法的結論（許容性/適法性/実質的変更/競争性/承認推奨/積算妥当性） | 出力する | **schema から構造排除** | 構造排除 | T10（士業法）リスク 🔴 高。決裁の最終判定は人間（決裁権者）に残す |
| S-3 の方式 | 断定語 denylist | **出力構造 allowlist + claim→source 実在検証** | allowlist 一本化 | denylist は言い換えで原理的にすり抜け false assurance になる |
| 顧客分離 | 行レベル `tenant_id` フィルタ | **接続レベル分離（顧客専用 DB/KB 接続）** | 接続レベル | 自治体ごとに文書管理・保持規程が異なり物理分離が実態に合う |
| 入力形式 | PDF/Excel も受理 | **軽量テキストのみ** | 軽量テキスト | パーサ増・OCR 崩れ・依存膨張を避ける |
