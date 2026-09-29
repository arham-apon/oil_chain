# Jev — Complete Developer Guide

> Source Reference: TypeSafe AI (`https://docs.typesafe.ai/introduction`)

---

## 1. What is Jev?

**Jev** is TypeSafe AI's flagship **System One** model.

Unlike conventional generative large language models (LLMs) such as GPT, Claude, or Gemini, Jev is built specifically to make fast, structured judgments that software can consume directly without text generation or parsing.

### Traditional LLM Workflow

```text
User Text Input ───► LLM ───► Generated Natural Language ───► Complex String/JSON Parsing ───► Application Logic
```

*Example problem:*
```text
User: "I was charged twice!"
LLM output: "This appears to be a billing issue. The customer is probably frustrated..."
Your code: Needs regex/parsing to extract "billing", "frustrated", and infer confidence.
```

### Jev System One Workflow

```text
State + Typed Questions ───► Jev ───► Directly Typed Answer Objects ───► Application Logic
```

*Example in Jev:*
```text
State: "I was charged twice!"
Question: "What department should handle this?"
Jev Output:
{
  "choice": "billing",
  "probabilities": { "billing": 0.88, "technical": 0.12, "sales": 0.0 },
  "confidence": 0.81
}
```

```python
if answer.choice == "billing":
    route_to_billing()
```

---

## 2. The Core Mental Model

Jev evaluates a given **State** against one or more **Typed Questions**.

```text
                  ┌── Choice Question ──► Selected Option + Probabilities + Confidence
                  │
State ────► Jev ──┼── Score Question  ──► Numeric Score + Probabilities + Confidence
                  │
                  └── Noul Question   ──► Probability (0.0 to 1.0)
```

### The Three Primitives Summary

| Primitive | Question Type | Output Values | Purpose |
| :--- | :--- | :--- | :--- |
| **`Choice`** | Which option from a set? | Selected option string, probability map, confidence float | Categorization, classification, tool selection |
| **`Score`** | Where does it land on a scale? | Weighted continuous score float, legend, probabilities, confidence float | Urgency, severity, sentiment, level |
| **`Noul`** | Is this true or false? | Probability float between $0.0$ and $1.0$ | Yes/No verification, boolean checks, policy compliance |

All three question types can be queried simultaneously in a single request and are evaluated independently against the same state.

---

## 3. Installation & Configuration

### Python SDK

Requires **Python 3.10+**.

```bash
pip install typesafe-sdk
# or using uv:
uv add typesafe-sdk
```

Set your API key in your environment:

```bash
# Linux / macOS
export TYPESAFE_API_KEY="your-api-key"

# Windows PowerShell
$env:TYPESAFE_API_KEY="your-api-key"
```

The SDK automatically resolves `TYPESAFE_API_KEY` from the environment. Avoid hard-coding API keys in source files.

### JavaScript / TypeScript SDK

Requires **Node.js 20+**.

```bash
npm install @typesafe-ai/sdk
```

---

## 4. Quick Start Example

```python
from typesafe_sdk import Choice, TypeSafeClient

ticket = "I was charged twice for my order. Please refund one of the charges."

with TypeSafeClient() as client:
    response = client.system_one(
        state=ticket,
        questions={
            "department": Choice(
                instructions="Which department should handle this ticket?",
                criteria={
                    "billing": "Payments, charges, refunds, or invoices",
                    "technical": "Bugs, errors, or technical problems",
                    "shipping": "Delivery, tracking, or shipping problems",
                },
            )
        },
    )

answer = response.answers["department"]
print(f"Choice: {answer.choice}")             # e.g., 'billing'
print(f"Probabilities: {answer.probabilities}") # e.g., {'billing': 0.88, ...}
print(f"Confidence: {answer.confidence}")       # e.g., 0.81
```

---

## 5. Understanding State

`state` is the context evaluated by Jev. State can be provided as:
1. **A raw string**
2. **A structured JSON object (dict)**
3. **A JSON array (list)**

> *Note:* Jev currently processes text-based context. Audio, video, and image inputs are not supported.

### Object State and Explicit Path Referencing

Using structured dictionary states allows questions to refer to nested fields using backticks (e.g., `'ticket.message'`):

```python
state = {
    "customer": {
        "name": "Arham",
        "plan": "enterprise"
    },
    "ticket": {
        "subject": "Duplicate payment",
        "message": "I was charged twice. Please refund one payment."
    },
    "order": {
        "id": "A-104",
        "amount": 49
    },
    "policy": {
        "refund_window_days": 30
    }
}

questions = {
    "refund_requested": Noul(
        instructions="Does `ticket.message` request a refund for order `order.id`?"
    ),
    "department": Choice(
        instructions="Which team should handle `ticket.message` based on customer tier `customer.plan`?",
        criteria={
            "enterprise_support": "Enterprise customer issues",
            "billing": "Standard billing and refund queries",
            "technical": "Technical failures and bugs",
        },
    ),
}
```

---

## 6. Deep Dive into the Primitives

### Primitive 1: `Choice`

Use `Choice` when the result must select an option from a predefined list (supports up to 255 items).

```python
Choice(
    instructions="What type of problem is this?",
    criteria={
        "billing": "Payment, subscription, or invoice issue",
        "technical": "Software bug or technical failure",
        "account": "Account access or profile issue",
        "other": "Does not fit any of the above categories",
    },
)
```

#### Best Practices for `Choice`
- **Provide rich descriptions:** Instead of `"billing": "billing"`, define `"billing": "Payment, subscription, or invoice issue"`.
- **Always provide an `"other"` fallback:** Covers unpredictable customer inputs without skewing standard classes.
- **Provide structured instructions if needed:**
  ```python
  Choice(
      instructions={
          "question": "Which department should handle this ticket?",
          "focus": "Determine the customer's primary problem.",
          "ignore": "Do not classify based only on customer emotion.",
      },
      criteria={
          "return_policy": {
              "covers": "Questions about whether an item can be returned.",
              "not_for": "Questions about existing open return packages.",
              "examples": ["Can I return opened items?"]
          },
          "return_status": {
              "covers": "Questions about an existing return process.",
              "not_for": "General return policy questions.",
              "examples": ["Where is my return refund?"]
          }
      }
  )
  ```

---

### Primitive 2: `Score`

Use `Score` when evaluating an ordered spectrum (between 2 and 10 ordered levels).

```python
Score(
    instructions="How severe is this bug?",
    criteria=[
        "Cosmetic: no impact on core functionality",
        "Degraded: broken feature, but a viable workaround exists",
        "Blocking: catastrophic failure with no workaround",
    ],
)
```

#### Understanding the Weighted Score Calculation
`Score` is **not** a discrete classification. It returns a probability-weighted continuous value:

$$\text{Score} = \sum_{i=0}^{n-1} (i \times P(\text{level}_i))$$

*Example:*
- Level 0 (Cosmetic): $P = 0.00$
- Level 1 (Degraded): $P = 0.57$
- Level 2 (Blocking): $P = 0.43$

$$\text{Score} = (0 \times 0.00) + (1 \times 0.57) + (2 \times 0.43) = 1.43$$

#### Response Attributes
```python
answer = response.answers["severity"]
print(answer.score)         # 1.43
print(answer.probabilities) # {0: 0.0, 1: 0.57, 2: 0.43}
print(answer.legend)        # {0: 'Cosmetic', 1: 'Degraded', 2: 'Blocking'}
print(answer.confidence)    # 0.35
```

---

### Primitive 3: `Noul`

Use `Noul` for yes/no verifications. It returns a continuous probability scalar from $0.0$ to $1.0$.

- **$0.99$**: Strong evidence for **Yes**
- **$0.05$**: Strong evidence for **No**
- **$0.50$**: High uncertainty (does *not* mean "half-true")

> `Noul` outputs probability directly and does not have a separate `.confidence` attribute.

```python
Noul(
    instructions="Does the customer qualify for a refund?",
    criteria={
        "true": "Customer has an active receipt and is within 30 days.",
        "false": "Customer purchase was marked non-refundable or exceeds window.",
    },
)
```

---

## 7. Probability vs. Confidence

| Metric | What It Means |
| :--- | :--- |
| **Probability** (`answer.probabilities`) | How model likelihood is distributed across candidates. |
| **Confidence** (`answer.confidence`) | How concentrated or definitive that probability distribution is. |

```text
Concentrated: [0.95, 0.05, 0.00] ──► High Confidence
Dispersed:    [0.40, 0.35, 0.25] ──► Low Confidence (Ambiguous)
```

### Confidence-Gated Automation Pattern

```python
answer = response.answers["department"]

if answer.confidence < 0.50:
    send_to_human_triage(ticket)
elif answer.choice == "billing":
    route_to_billing()
elif answer.choice == "technical":
    route_to_technical()
```

### Multi-Destination Probability Routing

```python
# Notify any team that exceeds a 25% threshold
for dept, prob in answer.probabilities.items():
    if prob > 0.25:
        notify_team(dept)
```

---

## 8. Composition Patterns

### 1. Atomic Decisions & Composite Scoring
Break complex subjective queries down into atomic metrics rather than prompting a model to give one combined answer:

```python
questions = {
    "severity": Score(instructions="Severity of technical outage", criteria=["Low", "Medium", "High"]),
    "customer_frustration": Score(instructions="Frustration level", criteria=["Calm", "Annoyed", "Hostile"]),
    "revenue_risk": Score(instructions="Commercial revenue impact", criteria=["Low", "Moderate", "Critical"])
}

# Application-level composite weighting
priority_index = (
    response.answers["severity"].score * 0.40 +
    response.answers["customer_frustration"].score * 0.20 +
    response.answers["revenue_risk"].score * 0.40
)
```

### 2. Speculative Fan-out
Batch multiple speculative queries together regardless of whether they ultimately apply:

```python
questions = {
    "department": Choice(...),
    "shipping_issue": Choice(...),
    "technical_severity": Score(...),
    "is_escalation": Noul(...),
}
```
If `department.choice != "technical"`, simply discard `technical_severity`. This is far faster and more cost-effective than running multiple round-trip calls sequentially.

### 3. Dependent Sequential Requests
Only issue sequential requests when question 2 requires database queries or operations triggered by question 1:

```text
Request 1 (Triage) ──► Get "shipping" ──► Query Tracking DB ──► Request 2 (Carrier Resolution)
```

---

## 9. End-to-End Implementation Examples

### Synchronous Python Customer Support Pipeline

```python
from typesafe_sdk import Choice, Noul, Score, TypeSafeClient

TRIAGE_QUESTIONS = {
    "department": Choice(
        instructions="Which department should handle this ticket?",
        criteria={
            "billing": "Payments, invoices, refunds",
            "technical": "Bugs, outages, errors",
            "shipping": "Delivery, status, carriers",
            "other": "Unrelated queries",
        },
    ),
    "refund_requested": Noul(
        instructions="Does the customer request a monetary refund?"
    ),
    "urgency": Score(
        instructions="How urgent is this issue?",
        criteria=["Low", "Medium", "Urgent", "Critical"],
    ),
}

def triage_ticket(ticket_text: str):
    with TypeSafeClient() as client:
        response = client.system_one(
            state=ticket_text,
            questions=TRIAGE_QUESTIONS,
        )

    dept = response.answers["department"]
    refund = response.answers["refund_requested"]
    urgency = response.answers["urgency"]

    if dept.confidence < 0.40:
        return {"action": "human_review", "reason": "Low classification confidence"}

    return {
        "department": dept.choice,
        "department_confidence": dept.confidence,
        "is_refund": refund.noul > 0.80,
        "urgency_score": urgency.score,
        "action": "automated_route",
    }
```

### Production FastAPI Service

```python
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul, Score

app = FastAPI(title="Ticket Routing Service")
jev_client = AsyncTypeSafeClient()

class TicketPayload(BaseModel):
    ticket_id: str
    message: str

@app.post("/triage")
async def handle_triage(payload: TicketPayload):
    try:
        response = await jev_client.system_one(
            state={"message": payload.message},
            questions={
                "department": Choice(
                    instructions="Select handling team",
                    criteria={
                        "billing": "Billing and invoice questions",
                        "tech_support": "Software and platform errors",
                        "general": "General questions",
                    },
                ),
                "is_urgent": Noul(instructions="Does this express urgency?"),
            },
        )

        dept_ans = response.answers["department"]
        urgent_ans = response.answers["is_urgent"]

        return {
            "ticket_id": payload.ticket_id,
            "department": dept_ans.choice,
            "confidence": dept_ans.confidence,
            "is_urgent": urgent_ans.noul > 0.70,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

### TypeScript / Node.js Implementation

```typescript
import { choice, noul, score, TypeSafeClient } from "@typesafe-ai/sdk";

const client = new TypeSafeClient();

async function routeUserMessage(userMessage: string) {
  const response = await client.systemOne({
    state: { message: userMessage },
    questions: {
      intent: choice("Identify customer intention", {
        refund: "Customer demands a refund",
        support: "Customer needs technical assistance",
        info: "Customer asking general inquiry",
      }),
      urgent: noul("Is this message urgent?"),
      frustration: score("Frustration level", ["Calm", "Frustrated", "Angry"]),
    },
  });

  const intent = response.answers.intent;
  const isUrgent = response.answers.urgent.noul > 0.75;
  const frustrationScore = response.answers.frustration.score;

  console.log(`Intent: ${intent.choice} (${intent.confidence})`);
  console.log(`Urgent: ${isUrgent}`);
  console.log(`Frustration Score: ${frustrationScore}`);
}
```

---

## 10. Direct HTTP API Reference

If you are not using an official SDK, you can integrate via standard HTTP calls.

### Endpoint
`POST https://api.typesafe.ai/v1/systemone`

### Headers
```http
Authorization: Bearer $TYPESAFE_API_KEY
Content-Type: application/json
```

### HTTP Status Codes
| Status | Meaning |
| :--- | :--- |
| `200` | Successful judgment execution |
| `401` | Unauthorized / missing or invalid API key |
| `422` | Unprocessable Entity / invalid schema, state, or criteria definition |
| `429` | Rate limit exceeded (SDKs support auto-retry) |
| `529` | System temporarily overloaded (retryable) |

### cURL Request Example

```bash
curl -X POST https://api.typesafe.ai/v1/systemone \
  -H "Authorization: Bearer $TYPESAFE_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "state": "I was charged twice for order #9822.",
    "model": "jev-latest",
    "questions": {
      "department": {
        "type": "choice",
        "instructions": "Which department should handle this?",
        "criteria": {
          "billing": "Payments, invoices, or charges",
          "shipping": "Order tracking or transit errors",
          "tech": "Technical platform errors"
        }
      },
      "wants_refund": {
        "type": "noul",
        "instructions": "Does the user explicitly ask for a refund?"
      },
      "frustration": {
        "type": "score",
        "instructions": "Rate customer frustration",
        "criteria": ["Calm", "Irritated", "Enraged"]
      }
    }
  }'
```

### HTTP JSON Response

```json
{
  "model": "jev-1.13.0",
  "answers": {
    "department": {
      "type": "choice",
      "choice": "billing",
      "probabilities": {
        "billing": 0.94,
        "shipping": 0.02,
        "tech": 0.04
      },
      "confidence": 0.91
    },
    "wants_refund": {
      "type": "noul",
      "noul": 0.98
    },
    "frustration": {
      "type": "score",
      "score": 1.12,
      "legend": {
        "0": "Calm",
        "1": "Irritated",
        "2": "Enraged"
      },
      "probabilities": {
        "0": 0.05,
        "1": 0.78,
        "2": 0.17
      },
      "confidence": 0.74
    }
  },
  "usage": {
    "input_tokens": 182,
    "output_tokens": 28
  }
}
```

---

## 11. System Architecture Combinations

### 1. Jev + Generative LLMs
Use Jev as a deterministic router or guardrail upfront. Call expensive LLMs only when open-ended natural language generation is strictly needed.

```text
User Request ──► Jev Intent Classifier
                      ├── Billing / DB Query ──► Execute Direct SQL / API
                      └── General Inquiry     ──► LLM (Claude / GPT / Gemini) ──► Prose Response
```

### 2. Jev + RAG Retrieval Pipeline
Rerank or filter chunks returned from vector search before context ingestion:

```python
# Pass each retrieved chunk as state to verify relevance
questions = {
    "is_relevant": Noul(
        instructions="Does this context document answer `user_query` directly?"
    )
}
# Only pass passages with answer.noul > 0.75 to the synthesizer model
```

### 3. Jev + AI Agent Tool Selection
Use `Choice` with tool criteria descriptions to select tools deterministically instead of unconstrained tool calling:

```python
tool_selector = Choice(
    instructions="Select the next tool execution based on the user request",
    criteria={
        "search_docs": "Lookup product specs and policies",
        "refund_tool": "Issue payment refund",
        "escalate": "Escalate to human rep",
        "none": "Respond directly without tools"
    }
)
```

---

## 12. Key Principles to Remember

1. **Jev makes judgments; your code makes decisions.** Never offload your core business rules and thresholds into a model prompt.
2. **Atomic questions beat composite prompts.** Evaluate small, clear properties and combine them using regular code arithmetic.
3. **Always batch questions on the same state.** Jev processes questions in parallel in one round-trip.
4. **Leverage confidence thresholds.** Route low-confidence events to human supervisors to eliminate hallucinations and misclassifications in automated loops.