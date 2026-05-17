"""Shared static reference material for Texas electricity regulatory prompts.

Appended to every Sonnet-tier system block (extract + compose) to bring the
cacheable prefix above Anthropic's 1,024-token minimum for claude-sonnet-4-6.

NOT used in classify() / haiku-gate: claude-haiku-4-5's cache minimum is 4,096
tokens — economically unreachable for a triage system prompt.

This constant must remain byte-identical across all calls. No f-string
interpolation, no per-call values.
"""


TEXAS_ELECTRICITY_TAXONOMY = """\
=== TEXAS ELECTRICITY REGULATORY REFERENCE ===

REGULATORY BODIES

Public Utility Commission of Texas (PUCT)
The PUCT is the state agency responsible for regulating investor-owned electric,
telecommunication, and water utilities in Texas. In the electricity sector the
PUCT regulates transmission and distribution utilities (TDUs), oversees the
competitive retail electricity market, certifies Retail Electric Providers (REPs),
and exercises oversight authority over the wholesale market administered by ERCOT.
The commission consists of three commissioners appointed by the governor. PUCT
staff and the Office of Policy Development investigate dockets and make
recommendations. Administrative Law Judges (ALJs) from the State Office of
Administrative Hearings (SOAH) preside over contested cases.

ERCOT (Electric Reliability Council of Texas)
ERCOT is the Independent System Operator (ISO) and Regional Transmission
Organization (RTO) serving approximately 90 percent of Texas electric load.
ERCOT operates the interconnection that is largely electrically isolated from
the rest of the continental US grid, giving Texas jurisdiction over its own
electric system outside most FERC oversight. ERCOT manages real-time grid
operations, energy and ancillary service markets, long-term resource adequacy
planning, and the retail customer switching process. ERCOT is governed by a
Board of Directors and a Technical Advisory Committee (TAC) composed of market
segment representatives.

FERC (Federal Energy Regulatory Commission)
FERC has limited jurisdiction in Texas because ERCOT's interconnection does not
cross state lines. FERC regulates hydroelectric licensing, natural gas pipelines,
and specific transmission assets that cross state boundaries. Texas REPs and TDUs
are not subject to FERC open-access tariff requirements that apply in other ISOs.

---

PUCT DOCUMENT TYPES

Control Number (CN): Every PUCT proceeding is assigned a control number
(e.g., CN 56789). All filings, orders, and correspondence in a docket are
indexed under that control number.

Application or Petition: Initial filing by a utility, REP, or other party
requesting PUCT action. Common applications include: certificates of convenience
and necessity (CCN) for new transmission facilities; rate case filings under
PUC Substantive Rule 25.231 (investor-owned electric utilities) or 25.241
(transmission); merger and acquisition approvals; and REP certification
applications under 25.107.

Order: Final or interim commission decision resolving issues in a docket. A final
order contains findings of fact, conclusions of law, and ordering paragraphs
specifying required actions or new rates. Interlocutory orders address procedural
or interim matters such as interim rates or discovery disputes.

Proposal for Decision (PFD): An ALJ's recommended decision issued in a contested
case after an evidentiary hearing. Parties may file exceptions to the PFD before
commissioners vote at an open meeting.

Response and Intervention: A filing by which a party (TDU, REP, OPUC, consumer
group, or competitor) enters a docket, responds to an application, or submits
comments on a proposed rule.

Compliance Filing: A post-order submission demonstrating that a utility or REP
has implemented the commission's directives, such as revised tariff sheets,
operational reports, or financial security adjustments.

Rulemaking Proceeding: The PUCT proposes and adopts rules codified in Title 16
of the Texas Administrative Code (16 TAC). Rulemaking dockets follow Texas
Government Code Chapter 2001 (Administrative Procedure Act) notice-and-comment
procedures with 30- or 45-day public comment periods.

Open Meeting Item: A docket placed on the commission's agenda for a vote at a
scheduled public open meeting. Minutes and recorded votes are filed in the docket
record.

---

ERCOT PROTOCOL REVISION DOCUMENT TYPES

ERCOT administers a formal revision process for its governing documents. Revision
requests are submitted by market participants or ERCOT staff, reviewed by the
Protocol Revision Subcommittee (PRS), voted on by TAC, and ultimately approved
or rejected by the ERCOT Board of Directors. PUCT has oversight authority but
does not directly vote on individual protocol revisions.

NPRR (Nodal Protocol Revision Request): An amendment to the ERCOT Nodal
Operating Protocols, which govern market operations, energy offers, settlement,
nodal pricing, and ancillary service procurement. NPRRs are numbered
sequentially, e.g., NPRR1301.

PGRR (Planning Guide Revision Request): A simultaneous amendment to the ERCOT
Planning Guide (and sometimes cross-referenced with Protocols).

NOGRR (Nodal Operating Guide Revision Request): An amendment to the ERCOT Nodal
Operating Guides, which provide operational procedures for ERCOT staff and market
participants.

SCR (Systemwide Change Request): A change to ERCOT system-wide business practice
manuals, typically covering EDI transaction formats, data exchange standards, or
operational workflows.

SMOGRR (Settlement Metering Operating Guide Revision Request): An amendment to
the Settlement Metering Operating Guide governing meter data submission
and validation.

RMGRR (Retail Market Guide Revision Request): An amendment to the Retail Market
Guide governing customer enrollment, switching timelines, and retail data
exchange between REPs and TDUs.

VCMRR (Verifiable Cost Methodology Revision Request): A change to the methodology
used for determining verifiable costs in ancillary service capacity markets.

OBDRR (Other Binding Documents Revision Request): used for non-protocol binding
docs (e.g., certain manuals).

---

ERCOT MARKET NOTICE CATEGORIES

ERCOT Market Notices (MNs) are operational communications distributed to market
participants. Each notice carries a unique MN identifier.

Market Operations Notices: Settlement run announcements, invoice corrections,
market clearing result adjustments, changes to market timelines, and Day-Ahead
Market (DAM) related communications.

System Operations Notices: Real-time system emergencies, Weather Watch
declarations, Energy Emergency Alert (EEA) activations, load shed events,
high-priority transmission outages, and generation resource dispatch
constraint notifications.

Retail Market Notices: Changes to customer registration processes, switching
timelines, Interval Data Recorder (IDR) requirements, or retail EDI transaction
formats affecting REPs and TDUs.

Planning and Interconnection Notices: Capacity, Demand, and Reserves (CDR)
report updates; transmission planning study results; generation interconnection
queue status changes; and Long-Term Transmission Planning (LTTP) updates.

---

ERCOT ENERGY AND ANCILLARY SERVICE MARKETS

Real-Time Market (RTM): Security-constrained economic dispatch that clears every
5-minute interval using Locational Marginal Prices (LMPs) at Settlement Points.

Day-Ahead Market (DAM): Forward energy and ancillary service market clearing for
the next operating day, establishing prices and award quantities before real-time.

Real-Time Co-Optimization (RTC or RTC+B): Simultaneous real-time optimization of
energy and ancillary services (plus battery-specific enhancements), went live
December 5, 2025.

Ancillary Services (AS):
- Regulation Up (RegUp) and Regulation Down (RegDn): Fast-responding resources
  providing Automatic Generation Control (AGC) to maintain grid frequency within
  bounds. Procured every hour in DAM.
- Responsive Reserve Service (RRS): Resources capable of 10-minute synchronous
  or non-synchronous response to arrest frequency deviations after a contingency.
- Non-Spinning Reserve (Non-Spin): Off-line resources available within 30 minutes
  to restore operating reserves.
- ERCOT Contingency Reserve Service (ECRS): A 10-minute headroom product
  introduced in June 2023 providing additional contingency response capacity
  without drawing down RRS.
- Fast Frequency Response (FFR): Sub-second response from inverter-based
  resources (IBRs) to arrest initial frequency decline before governor response.

Congestion Revenue Rights (CRRs): Financial instruments auctioned by ERCOT that
allow market participants to hedge transmission congestion between Settlement
Points. CRRs are linked to the difference in LMPs across constrained paths.

Settlement Points and Nodes: ERCOT prices energy at Hub Settlement Points
(e.g., HB_NORTH, HB_SOUTH, HB_WEST, HB_HOUSTON), Load Zone Settlement Points,
and individual Resource Nodes for nodal generators.

---

TEXAS ELECTRICITY MARKET PARTICIPANTS

Retail Electric Providers (REPs): PUCT-certified entities that purchase wholesale
electricity and sell it to retail customers in the competitive (deregulated) zones
of the ERCOT market. REPs must maintain financial security deposits with the PUCT.

Transmission and Distribution Utilities (TDUs / TDSPs): Regulated monopoly
utilities that own and operate distribution and transmission infrastructure
delivering electricity to customers. The four investor-owned TDUs in ERCOT are:
Oncor Electric Delivery; CenterPoint Energy Houston Electric; AEP Texas (covering
AEP Texas Central and AEP Texas North service territories); and Texas-New Mexico
Power (TNMP).

Qualified Scheduling Entities (QSEs): ERCOT-certified entities that submit energy
offers, ancillary service bids, and load forecasts on behalf of generation
resources and loads. Every resource connected to the ERCOT grid must be
represented by a QSE.

Load Serving Entities (LSEs) and Load Resources (LRs): Entities representing
controllable demand-response resources capable of reducing consumption in response
to ERCOT dispatch signals or price incentives.

Municipally Owned Utilities (MOUs) and Electric Cooperatives: Several large Texas
utilities operate outside the competitive retail market under separate regulatory
frameworks. CPS Energy (San Antonio) and Austin Energy are major MOUs.
Cooperative utilities such as Pedernales Electric Cooperative serve rural areas
and may or may not be subject to full PUCT jurisdiction.

Office of Public Utility Counsel (OPUC): A state agency that represents
residential and small commercial consumers in PUCT proceedings. OPUC regularly
intervenes in rate cases, rulemaking dockets, and merger proceedings to protect
consumer interests.

---

COMMON REGULATORY TERMS

Effective Date: The date on which a PUCT order, tariff change, or ERCOT protocol
revision takes legal effect and must be implemented by the affected party.

Comment or Action Deadline: A specified date by which parties must file comments,
interventions, protests, or other pleadings. Missing a deadline typically
constitutes waiver of the right to participate on that issue.

Tariff: A utility's schedule of rates, terms, and conditions filed with and
approved by the PUCT. TDU tariffs govern distribution access charges (DACs) and
transmission cost of service (TCOS) pass-through to REPs and customers.

Rate Case: A formal proceeding in which a TDU requests a change to its regulated
distribution or transmission rates, filed under PUC Substantive Rules 25.231 or
25.241 and governed by PURA Chapters 36-37.

Resource Adequacy (RA): The ability of the ERCOT generation fleet to serve peak
demand with an adequate reserve margin. ERCOT monitors RA through the CDR and
Seasonal Assessment of Resource Adequacy (SARA) reports.

Performance Credit Mechanism (PCM): An ERCOT forward capacity construct adopted
after Winter Storm Uri (February 2021) to compensate resources that demonstrate
availability during scarcity events, incentivizing weatherization and reliability
investment.
(Note: The PUCT formally shelved the PCM design in December 2024, citing
insufficient reliability benefits relative to cost and ongoing market changes
such as RTC.)

Winter Storm Uri (February 2021): Severe winter weather that caused widespread
generation failures and customer outages across Texas; a defining reference event
for subsequent regulatory proceedings addressing weatherization requirements,
ERCOT market reforms, and grid reliability standards under SB 3 and SB 2.

=== END TEXAS ELECTRICITY REGULATORY REFERENCE ==="""
