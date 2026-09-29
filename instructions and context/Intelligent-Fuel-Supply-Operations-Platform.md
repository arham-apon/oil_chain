

## Intelligent Fuel Supply Operations Platform

## BUP CSE FEST 2026 | Hackathon Finals — Comprehensive Technical Documentation

## 1. Executive Summary & Challenge Overview

The BUP CSE FEST 2026 Hackathon Finals challenges participating teams to engineer an end-to-end, resilient Fuel Supply Operations Platform operating on top of a shared, organizer-provided simulator.

- Context: Bangladesh's simulated national fuel supply chain consists of interdependent components including supply import points, storage depots, distribution routes, administrative regions, fuel stations, and consumer demand. A disruption at any node cascades throughout the supply network—e.g., a shipment delay reduces depot inventory, sudden demand surges trigger regional stockouts, and transport route blockages invalidate planned distribution paths.

- Core Objective: Build a deployable operational platform capable of observing network conditions, predicting shortages, optimizing constrained allocations, reacting dynamically to disruptions, explaining decisions to human operators, maintaining high resilience under failure, and demonstrating measurable performance under load.

- Operational Boundary: All solutions operate strictly against the oýcial BUP Fuel Supply Simulator. No real-world infrastructure is accessed, and no actual fuel dispatches or ýnancial transactions are executed.

| Dimension Specification |
| --- |
| Core Goal Build a working fuel operations decision-support platform on top of the simulator. Must Operator-facing application, backend service, intelligence engine, deployment setu Include p, observability stack, resilience patterns, and load testing. |
| Simulator Common BUP Fuel Supply Simulator accessed via documented APIs. |


| Dimension | Specification |
| --- | --- |
| Dynamic | Organizers inject real-time crisis scenarios, demand spikes, and infrastructure failu |
| Element | res during evaluation. |
| Primary | Observe Detect Predict Decide Simulate Act Monitor Recove → → → → → → → |
| Paradigm | r. |

## 2. Supply Chain Architecture & Domain Model

The platform models and manages the downstream and midstream operational lifecycle across three primary fuel types:

- Diesel

- Petrol

- Octane

(Multi-tier simulated fuel supply ýow)

## The Core Engineering Loop

The system must sustain an active closed loop:

- 1. Observe: Continuously poll and ingest telemetry from the simulator regarding depot/station inventory and incoming shipments.

- 2. Detect: Flag inventory depletion rates, route bottlenecks, and sudden spikes in consumption.

- 3. Predict: Forecast future demand curves, stockout timelines (hours to empty), and transit delay risks.

- 4. Decide: Compute optimal distribution orders prioritizing critical shortfalls under constrained capacity.


- 5. Simulate: Project the post-action network impact prior to executing the allocation.

- ý. Act: Dispatch orders to the simulator ( POST /allocations ) or queue them for operator sign-off.

- 7. Monitor: Track real-time network recovery and key performance indicators.

- ý. Recover: Gracefully handle failed dependencies, degraded inputs, and route re-planning.

## 3. Organizer-Provided Simulator Integration

Teams do not build the simulation engine. The organizers provide the BUP Fuel Supply Simulator exposing RESTful endpoints:

## Conceptual API Endpoints

- GET /stations : Returns fuel station metadata, current inventory per fuel type, capacity, and geographic coordinates.

- GET /depots : Ingests central storage facility states, current reserves, and throughput limits.

- GET /supply-arrivals : Provides tracking for inbound vessel/reýnery fuel shipments, ETAs, and cargo volumes.

- GET /demand-history : Historical and immediate fuel draw logs across regions.

- GET /routes : Network connectivity, distance, travel constraints, transit times, and transit status.

- GET /events : Disruption logs, weather incidents, route blockades, and systemic alerts.

- POST /allocations : Submits dispatch orders (source depot, destination station, fuel type, volume, and route).

## 4. System Capabilities & Technical Requirements

## 4.1 Operator-Facing Application

A Jupyter Notebook alone is explicitly disallowed as a ýnal submission. Teams must deliver a responsive, production-ready graphical interface (web application recommended).

- Inventory & Asset Visualization: Dynamic status indicators for depots, fuel stations, and regional reserve levels.

- Demand & Shortage Tracking: Geo-spatial or tabular representation of regional consumption rates and predicted shortage risks.

- Supply & Disruption Feed: Visibility into incoming cargo, delayed arrivals, and route closures.

- Actionable Decision Center: Interface displaying system recommendations, projected risk mitigation, decision history, and operator override controls.

- System Health Console: Real-time visibility into microservice health, backend availability, and API latency.


## 4.2 Intelligence Engine Requirements

Every solution must implement at least one meaningful, high-utility intelligence capability:

│

Platform Intelligence Engine

│

Platform Intelligence Engine

│

│

│

│

│

Detection

│

│ Prediction │

│ Deci

Prediction

sion / Optimize │

│• Anomalous demand │

│• Demand forecasting │

│• Cons

trained opt. │

│• Abnormal inventory │

│• Shortage prediction │

│• Prio

rity allocation │

│• Network bottlenecks │

│• Stockout prob.

│• Heur

│

Stockout prob.

istic dispatch │

│• Regional disruption │

│• Supply ETA & delays │

│• Rein

forcement learn.│

│

│

│

v

│

Generative AI

│

Generative AI

│• Supply chain state summarization │

│• Human-readable incident breakdown │

│• Operator investigation copilot │

- Reinforcement Learning (Optional): RL may be used for sequential allocation decisions. If chosen, teams must explicitly formulate:

- State: Current inventory, regional demand, predicted shortages, available depot reserves, and route status.

- Action: Fuel volume to allocate, destination station, transit route selection, or allocation deferral.


- Reward: expenses, and maximization of service levels. Penalty for unmet demand, penalty for stockout events, minimization of transit

- Benchmarking: Teams must demonstrate why RL outperforms deterministic heuristics or linear programming.

- Generative AI: LLM implementations must serve operational workýowsgenerating concise incident summaries, diagnostic reasoning, and decision justiýcations-rather than functioning as an ungrounded chatbot.

## 4.3 Decision Support & Explainability

Automated recommendations must be inspectable and veriýable by operational dispatchers.

## Decision Inspectability Schema Example:

- Alert Entity: Station DHAKA-021

Target Fuel: Diesel

Projected Stockout: 6.2 hours

Current Inventory: 8,400 L

Expected Demand: 11,900 L

Recommended Allocation: 5,000 L sourced from DEPOT-03

- Expected Result: Stockout risk reduced from 72% down to 19%

- Explainability Factors: Signals inýuencing priority, constraint criteria (e.g., driver hours, tank capacity), conýdence bounds, and alternative fallback dispatches.

## 5. Crisis Handling, Resilience & DevOps

## 5.1 Dynamic Crisis Scenarios

The platform must identify, adapt to, and recover from real-time operational shocks introduced during evaluation:

| Crisis Scen |   |
| --- | --- |
| Injected Condition | Expected System Response |
| ario |   |
| Shipment Incoming fuel shipment arrive | Raise early warnings, compute shortage impact, |
| Delay s behind schedule. | reprioritize reserves, track recovery. |
| Demand Sudden consumption surge in | Detect anomaly, adjust forward forecasts, re-rou |
| Spike one or more zones. | te supply to prevent localized collapse. |
| Depot Depot storage capacity or pu | Re-evaluate allocation constraints, divert supply |
| Constraint mp throughput degraded. | runs to alternate depots. |


| Crisis Scen |   |
| --- | --- |
| Injected Condition | Expected System Response |
| ario |   |
| Regional Critical transit route or sector c | Recalculate routing paths, activate alternative l |
| Disruption losed. | ogistics channels. |
| Combined Simultaneous compound even | Maintain stability, operate within failure bounda |
| Crisis ts (e.g., spike + delay). | ries, maintain critical lifelines. |

## 5.2 Application Resilience & Degradation Paths

The software architecture must implement explicit safety fallbacks when technical components fail:

- ML Model Service Down: Fall back to a deterministic, rule-based heuristic allocation policy.

- Simulator API Invalid / Malformed Data: Reject corrupted payloads, maintain last valid cache, and trigger an operator alert.

- Low Prediction Conýdence: Flag decision for mandatory human operator review before dispatch.

- Backend Dependency Failure: Activate circuit breakers, utilize cached states, and enter degraded operational mode.

## 5.3 DevOps & Observability Standards

- Deployment Baseline: Complete platform must be reproducible locally using a single command:

- CI/CD Lifecycle: Structured software delivery pipeline: Source Code⟶Build⟶ Test⟶Package⟶Deploy⟶Health Check⟶Running Application


## System Observability Stack:

- Application Layer: Request rates, latency proýles, error rates, service uptime.

- Infrastructure Layer: CPU usage, RAM utilization, storage I/O.

- Model / Intelligence Layer: Prediction errors, conýdence scores, alert trigger frequency, fallback activations.

- Audit Logs: Allocation execution logs, network integration failures, operator overrides.

- Component Health Dashboard: Expose a dedicated diagnostic view showing operational status across:

- Backend REST API ( Healthy / Degraded / Down )

- Database Engine

- Simulator Connection

- Inference Service

- Decision Optimization Engine

- Live Telemetry: p95 Latency (ms) and System Error Rate (%)

## 5.4 Performance & Load Veriýcation

Teams must execute load testing against at least one critical application path (e.g., Decision API, Prediction API, or Simulator Ingestion Pipeline).

- Mandatory Reported Metrics: Average latency, p50 p95 , , and p99 response times, total throughput (RPS), error rate percentages, concurrency levels, and peak hardware utilization.

## 6. Deliverables, Evaluation & Suggested Demo Flow

- 6.1 Required Deliverables Checklist

- 1. Working Application: Fully operational platform with functional UI and backend.

- 2. Source Repository: Complete codebase, environment conýguration ýles, dependency deýnitions, and setup documentation.

- 3. Simulator Integration: Veriýable bi-directional communication with the BUP Fuel Supply Simulator.

- 4. Intelligence Component: Working AI/ML forecasting, optimization, or heuristic decision engine.

- 5. Operator Interface: Usable dashboard displaying operational state and allocations.

- ý. Architecture Diagram: Comprehensive diagram covering data ingestion, intelligence, decision engines, UI, and observability.

- 7. Deployment Package: Containerized setup script (e.g., Docker Compose).

- ý. Observability Evidence: Live metrics dashboards, structured logging, and health checks.


- 9. Resilience Demonstration: Live proof of graceful degradation during a component failure.

- 10. Load-Test Evidence: Benchmarking run data, latency distributions, and saturation limits.

- 11. Final Live Demonstration: End-to-end operational walkthrough presented to the judges.

## 6.2 Recommended & Advanced Scope

- Recommended: CI/CD pipelines (GitHub Actions/GitLab CI), automated unit/integration tests, model experiment tracking, scenario replay capabilities, and automated rollback.

- Advanced (Optional): Kubernetes deployments, Helm charts, Terraform IaC, distributed message brokers (Kafka/RabbitMQ), streaming state stores, and multi-agent coordination.

## 6.3 Evaluation Rubric

- Working Product & User Experience (20%): Completeness of end-to-end workýows, interface usability, clarity of operations.

- Intelligence & Decision Quality (20%): Methodological soundness of detection, forecasting, and optimization logic.

- Architecture & Integration (15%): Clean backend separation, simulator API compliance, technical robustness.

- DevOps & Engineering Quality (15%): Containerization, clean repo hygiene, CI/CD pipelines, documentation, zero hardcoded secrets.

- Resilience & Incident Response (10%): Behavior during injected disruptions, fallback stability, error containment.

- Observability & Performance (10%): Depth of telemetry metrics, log coverage, health endpoints, empirical load test validation.

- Demo & Problem Understanding (10%): Effective live defense, architectural articulation, handling judge QA.

## 7. Step-by-Step Suggested Demonstration Storyline

Teams should structure their ýnal judge presentation around this 14-step narrative:


- ──► Display baseline steady-state network conditions.

- 1. Normal Operations

- ──► Walk through map views, depot levels, and incoming s

- 2. Operator Dashboard hipments.

- ──► Simulator introduces elevated consumption in a targe

- 3. Demand Increase t zone.

- ──► Telemetry flags inventory depletion anomaly.

- 4. Risk Detection

- 5. Intelligence Prediction ──► Model projects stockout in hours and calculates prob

- ability.

- 6. Recommendation Created ──► Optimization engine formulates a constrained dispatc

- h order.

- 7. Operator Inspection ──► Operator examines the recommendation, confidence, an

- d constraints.

- 8. Simulation Execution ──► Impact is simulated and approved; order is dispatche

- d to simulator.

- ──► Organizer injects a severe shock (e.g., route cut or

- 9. Crisis Injected depot failure).

- ──► Engine re-routes transit and updates alternative all

- 10. Dynamic Adaptation ocations.

- 11. Dependency Injected Fail ──► Primary inference engine or microservice dependency

- is killed.

- ──► Observability stack surfaces degradation alerts imme

- 12. Monitoring Alarm diately.

- 13. Fallback Activation ──► System degrades safely into cached / heuristic rule-

- based policy.

- 14. Operations Continue ──► Core platform remains operational, stable, and respo

- nsive.

## 8. Operational Guardrails & Success Formula

## Strict Hackathon Constraints

- All operations must run strictly within the simulated environment; never interact with real industrial control systems or fuel supply infrastructure.

- Never initiate real commercial transactions or actual fuel dispatches.

- Never store or commit real-world secrets, operational tokens, or credentials into source repositories.

- Clearly differentiate simulation telemetry from real-world fuel data.

- Retain human oversight for consequential dispatch actions.

## The Winning Formulation

\text{Success} &= \text{Useful Application} \\ &+ \text{Meaningful Intelligence} \\ &+ \text{Reliable Backend} \\ &+ \text{Reproducible Deployment} \\ &+ \text{Deep Observability} \\ &+ \text{Fault Resilience} \\ &+ \text{Measured Performance under Load} \end{aligned}\$\$ The highest scoring solution is not measured by model complexity alone, but by the team's ability to turn predictive intelligence into an **engineered, observable, resilient, and operational system**.
