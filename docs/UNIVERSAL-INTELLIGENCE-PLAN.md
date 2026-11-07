# JARVIS Universal Intelligence & Execution System

**Status:** In Progress | **Owner:** Topaz | **Last updated:** 2026-11-03

## Vision
Natural conversation → autonomous planning → real execution → verified results. JARVIS understands complex requests and automatically selects capabilities, researches, allocates resources, develops/tests, and reports results.

## Architecture
- Natural Language Task Engine (planner) - converts conversation to structured tasks
- Universal Capability Registry - auto-discovers and routes to capabilities
- Hardware-aware Compute Scheduler - routes to PC-001 (RTX 3080), APOS (GTX 1660S), LAPTOP-001
- ML/Neural Network Lab - training, evaluation, deployment
- CHIMERA Lab - game engines, modding, interoperability, reverse engineering
- Integration with existing core, tools, policy, memory, nodes

## Components Added (V3.2)
- `jarvis/planner/` - TaskEngine for natural language parsing
- `jarvis/capabilities/` - Universal capability registry with auto-detection
- `jarvis/ml_lab/` - ML laboratory scaffolding
- `jarvis/chimera/` - CHIMERA game engineering lab
- `jarvis/scheduler/` - Hardware-aware compute scheduler
- Registered in kernel as optional services

## Principles
- Conversation first - no manual prompt selection
- Execution over explanation when tools/authorization available
- Local-first where possible
- Safe autonomy with approval gates for destructive actions
- Verification required
- Hardware adaptation
- Cross-platform support

## Next Steps
- Integrate planner with agent turn loop for automatic capability selection
- Connect prompts library to capability system
- Add Godot/Blender tool integrations
- Implement PyTorch training scaffolding
- Add resource monitoring for adaptive scheduling
- Expand verification and testing

## Core Rule
PC-001 is a client/agent (HUD, voice, GPU for training/rendering). Core runs on APOS.
