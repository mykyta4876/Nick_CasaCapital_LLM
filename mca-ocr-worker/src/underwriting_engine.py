"""
MCA Underwriting Rules Engine
Evaluates applications against configurable underwriting guidelines
Outputs: Program eligibility, max funding, buy rates, risk score, decision

All rules are configurable via config/underwriting_rules.json
"""

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum


class Decision(Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    AUTO_DECLINE = "AUTO_DECLINE"


@dataclass
class ApplicationData:
    """Input application data"""
    # Business info
    business_name: str = ""
    time_in_business_years: float = 0
    industry: str = ""
    state: str = ""
    
    # Owner info
    fico_score: int = 0
    
    # Bank statement metrics (from MoneyThumb analysis)
    monthly_revenue: float = 0
    true_monthly_revenue: float = 0
    monthly_expenses: float = 0
    avg_daily_balance: float = 0
    negative_days: int = 0
    nsf_count: int = 0
    deposit_days_per_month: int = 0
    
    # Existing MCA positions
    existing_positions: int = 0
    mca_withhold_percent: float = 0
    total_mca_payments_monthly: float = 0
    
    # Request
    requested_amount: float = 0
    use_of_funds: str = ""
    
    # Additional flags
    has_national_bank: bool = True
    has_contractor_license: bool = False
    tradelines_count: int = 0
    trucks_on_safer: int = 0
    has_insurance_binder: bool = False


@dataclass
class DeclineReason:
    """Reason for decline or flag"""
    rule: str
    message: str
    value: Any
    threshold: Any


@dataclass
class ProgramOffer:
    """Offer for a specific program/position"""
    program_name: str
    position: int
    max_funding: float
    max_term_months: float
    buy_rate: float
    sell_rate: float
    daily_payment: float
    weekly_payment: float
    payback_amount: float
    origination_fee_percent: float
    origination_fee_amount: float
    commission_percent: float
    commission_amount: float
    gross_reserve_percent: float


@dataclass
class UnderwritingResult:
    """Complete underwriting decision"""
    # Decision
    decision: str = ""
    risk_score: int = 0
    
    # Decline reasons (if any)
    decline_reasons: List[DeclineReason] = field(default_factory=list)
    flags: List[DeclineReason] = field(default_factory=list)
    
    # Eligible programs
    eligible_programs: List[str] = field(default_factory=list)
    ineligible_programs: Dict[str, List[str]] = field(default_factory=dict)
    
    # Offers (best to worst)
    offers: List[ProgramOffer] = field(default_factory=list)
    
    # Recommended offer
    recommended_offer: Optional[ProgramOffer] = None
    
    # Stips required
    stips_required: List[str] = field(default_factory=list)
    
    # Summary
    max_approved_amount: float = 0
    position: int = 0
    
    # Timestamps
    evaluated_at: str = ""


class UnderwritingEngine:
    """Main underwriting engine"""
    
    def __init__(self, config_path: str = None):
        """Load config from file or use defaults"""
        if config_path and Path(config_path).exists():
            with open(config_path, 'r') as f:
                self.config = json.load(f)
        else:
            # Load from default location
            default_path = Path(__file__).parent.parent / "config" / "underwriting_rules.json"
            if default_path.exists():
                with open(default_path, 'r') as f:
                    self.config = json.load(f)
            else:
                raise FileNotFoundError(f"Config not found at {config_path} or {default_path}")
    
    def evaluate(self, app: ApplicationData) -> UnderwritingResult:
        """Evaluate an application and return underwriting decision"""
        result = UnderwritingResult()
        result.evaluated_at = datetime.now().isoformat()
        
        # Step 1: Check auto-decline triggers
        decline_reasons = self._check_auto_decline(app)
        if decline_reasons:
            result.decision = Decision.AUTO_DECLINE.value
            result.decline_reasons = decline_reasons
            result.risk_score = 0
            return result
        
        # Step 2: Calculate risk score
        result.risk_score, result.flags = self._calculate_risk_score(app)
        
        # Step 3: Determine eligible programs
        result.eligible_programs, result.ineligible_programs = self._check_program_eligibility(app)
        
        if not result.eligible_programs:
            result.decision = Decision.AUTO_DECLINE.value
            result.decline_reasons = [
                DeclineReason(
                    rule="no_eligible_programs",
                    message="No programs available for this application",
                    value=None,
                    threshold=None
                )
            ]
            return result
        
        # Step 4: Calculate position based on existing MCAs
        position = app.existing_positions + 1
        result.position = position
        
        # Step 5: Generate offers for each eligible program
        result.offers = self._generate_offers(app, result.eligible_programs, position)
        
        if not result.offers:
            result.decision = Decision.AUTO_DECLINE.value
            result.decline_reasons = [
                DeclineReason(
                    rule="no_valid_offers",
                    message=f"Position {position} not available in any eligible program",
                    value=position,
                    threshold=None
                )
            ]
            return result
        
        # Step 6: Select recommended offer (best terms)
        result.recommended_offer = self._select_best_offer(result.offers)
        result.max_approved_amount = result.recommended_offer.max_funding
        
        # Step 7: Determine stips
        result.stips_required = self._get_required_stips(result.max_approved_amount)
        
        # Step 8: Final decision
        scoring = self.config.get("risk_scoring", {})
        if result.risk_score < scoring.get("auto_decline_below", 40):
            result.decision = Decision.AUTO_DECLINE.value
        elif result.risk_score < scoring.get("manual_review_below", 70):
            result.decision = Decision.MANUAL_REVIEW.value
        else:
            result.decision = Decision.AUTO_APPROVE.value
        
        return result
    
    def _check_auto_decline(self, app: ApplicationData) -> List[DeclineReason]:
        """Check auto-decline triggers"""
        reasons = []
        triggers = self.config.get("auto_decline_triggers", {})
        
        # Max negative days
        max_neg = triggers.get("max_negative_days", 5)
        if app.negative_days >= max_neg:
            reasons.append(DeclineReason(
                rule="max_negative_days",
                message=f"Negative balance days ({app.negative_days}) exceeds maximum ({max_neg})",
                value=app.negative_days,
                threshold=max_neg
            ))
        
        # Max NSF count
        max_nsf = triggers.get("max_nsf_count", 10)
        if app.nsf_count > max_nsf:
            reasons.append(DeclineReason(
                rule="max_nsf_count",
                message=f"NSF count ({app.nsf_count}) exceeds maximum ({max_nsf})",
                value=app.nsf_count,
                threshold=max_nsf
            ))
        
        # Min monthly deposits
        min_deposits = triggers.get("min_monthly_deposits", 20000)
        if app.monthly_revenue < min_deposits:
            reasons.append(DeclineReason(
                rule="min_monthly_deposits",
                message=f"Monthly deposits (${app.monthly_revenue:,.2f}) below minimum (${min_deposits:,.2f})",
                value=app.monthly_revenue,
                threshold=min_deposits
            ))
        
        # Max MCA withhold percent
        max_withhold = triggers.get("max_mca_withhold_percent", 0.50)
        if app.mca_withhold_percent > max_withhold:
            reasons.append(DeclineReason(
                rule="max_mca_withhold_percent",
                message=f"MCA withhold ({app.mca_withhold_percent:.1%}) exceeds maximum ({max_withhold:.1%})",
                value=app.mca_withhold_percent,
                threshold=max_withhold
            ))
        
        # Max existing positions
        max_positions = triggers.get("max_existing_positions", 6)
        if app.existing_positions >= max_positions:
            reasons.append(DeclineReason(
                rule="max_existing_positions",
                message=f"Existing positions ({app.existing_positions}) at or exceeds maximum ({max_positions})",
                value=app.existing_positions,
                threshold=max_positions
            ))
        
        # Min deposit days
        min_deposit_days = triggers.get("min_deposit_days_per_month", 4)
        if app.deposit_days_per_month < min_deposit_days:
            reasons.append(DeclineReason(
                rule="min_deposit_days",
                message=f"Deposit days ({app.deposit_days_per_month}) below minimum ({min_deposit_days})",
                value=app.deposit_days_per_month,
                threshold=min_deposit_days
            ))
        
        return reasons
    
    def _calculate_risk_score(self, app: ApplicationData) -> Tuple[int, List[DeclineReason]]:
        """Calculate risk score 0-100"""
        scoring = self.config.get("risk_scoring", {})
        weights = scoring.get("weights", {})
        base = scoring.get("base_score", 100)
        
        score = base
        flags = []
        
        # Negative days penalty
        if app.negative_days > 0:
            penalty = app.negative_days * weights.get("negative_days", -10)
            score += penalty
            flags.append(DeclineReason(
                rule="negative_days",
                message=f"{app.negative_days} negative balance days",
                value=app.negative_days,
                threshold=0
            ))
        
        # NSF penalty
        if app.nsf_count > 0:
            penalty = app.nsf_count * weights.get("nsf_count", -5)
            score += penalty
            flags.append(DeclineReason(
                rule="nsf_count",
                message=f"{app.nsf_count} NSF/overdraft transactions",
                value=app.nsf_count,
                threshold=0
            ))
        
        # MCA positions penalty
        if app.existing_positions > 0:
            penalty = app.existing_positions * weights.get("mca_positions", -8)
            score += penalty
            flags.append(DeclineReason(
                rule="existing_mca",
                message=f"{app.existing_positions} existing MCA positions",
                value=app.existing_positions,
                threshold=0
            ))
        
        # MCA withhold penalties
        if app.mca_withhold_percent > 0.25:
            score += weights.get("mca_withhold_over_25_percent", -25)
            flags.append(DeclineReason(
                rule="high_mca_withhold",
                message=f"MCA withhold at {app.mca_withhold_percent:.1%} (>25%)",
                value=app.mca_withhold_percent,
                threshold=0.25
            ))
        elif app.mca_withhold_percent > 0.15:
            score += weights.get("mca_withhold_over_15_percent", -15)
            flags.append(DeclineReason(
                rule="moderate_mca_withhold",
                message=f"MCA withhold at {app.mca_withhold_percent:.1%} (>15%)",
                value=app.mca_withhold_percent,
                threshold=0.15
            ))
        
        # Bonuses
        if app.existing_positions == 0:
            score += weights.get("no_mca_positions", 15)
        
        if app.monthly_revenue > 100000:
            score += weights.get("high_revenue", 10)
        
        profit = app.true_monthly_revenue - app.monthly_expenses
        if profit > 0:
            score += weights.get("positive_profit", 10)
        
        # Clamp score
        score = max(0, min(100, score))
        
        return score, flags
    
    def _check_program_eligibility(self, app: ApplicationData) -> Tuple[List[str], Dict[str, List[str]]]:
        """Check which programs the application qualifies for"""
        eligible = []
        ineligible = {}
        
        programs = self.config.get("programs", {})
        
        for prog_key, prog in programs.items():
            if not prog.get("enabled", True):
                continue
            
            reasons = []
            
            # Time in business
            min_tib = prog.get("min_time_in_business_years", 0)
            if app.time_in_business_years < min_tib:
                reasons.append(f"Time in business ({app.time_in_business_years}y) below minimum ({min_tib}y)")
            
            # FICO score
            min_fico = prog.get("min_fico", 0)
            if app.fico_score < min_fico:
                reasons.append(f"FICO ({app.fico_score}) below minimum ({min_fico})")
            
            # Deposit days
            min_dep_days = prog.get("min_deposit_days_per_month", 0)
            if app.deposit_days_per_month < min_dep_days:
                reasons.append(f"Deposit days ({app.deposit_days_per_month}) below minimum ({min_dep_days})")
            
            # Monthly deposits
            min_monthly = prog.get("min_monthly_deposits", 0)
            if app.monthly_revenue < min_monthly:
                reasons.append(f"Monthly deposits (${app.monthly_revenue:,.0f}) below minimum (${min_monthly:,.0f})")
            
            # Negative days
            max_neg = prog.get("max_negative_days", 999)
            if app.negative_days > max_neg:
                reasons.append(f"Negative days ({app.negative_days}) exceeds maximum ({max_neg})")
            
            # National bank requirement
            if prog.get("requires_national_bank", False) and not app.has_national_bank:
                reasons.append("Requires national bank account")
            
            # Contractor license
            if prog.get("requires_contractor_license", False) and not app.has_contractor_license:
                reasons.append("Requires contractor license")
            
            # Tradelines
            min_trades = prog.get("min_tradelines", 0)
            if min_trades > 0 and app.tradelines_count < min_trades:
                reasons.append(f"Tradelines ({app.tradelines_count}) below minimum ({min_trades})")
            
            # Trucks on SAFER (trucking program)
            min_trucks = prog.get("min_trucks_on_safer", 0)
            if min_trucks > 0 and app.trucks_on_safer < min_trucks:
                reasons.append(f"Trucks on SAFER ({app.trucks_on_safer}) below minimum ({min_trucks})")
            
            # Insurance binder
            if prog.get("requires_insurance_binder", False) and not app.has_insurance_binder:
                reasons.append("Requires insurance binder")
            
            # Industry check (for specialized programs)
            industry_codes = prog.get("industry_codes", [])
            if industry_codes:
                app_industry = app.industry.lower().replace(" ", "_")
                if not any(code in app_industry for code in industry_codes):
                    reasons.append(f"Industry '{app.industry}' not eligible for this program")
            
            if reasons:
                ineligible[prog.get("name", prog_key)] = reasons
            else:
                eligible.append(prog_key)
        
        return eligible, ineligible
    
    def _generate_offers(self, app: ApplicationData, eligible_programs: List[str], position: int) -> List[ProgramOffer]:
        """Generate offers for eligible programs at the given position"""
        offers = []
        programs = self.config.get("programs", {})
        calc = self.config.get("funding_calculation", {})
        
        for prog_key in eligible_programs:
            prog = programs.get(prog_key, {})
            positions = prog.get("positions", {})
            pos_config = positions.get(str(position), {})
            
            if not pos_config.get("enabled", False):
                continue
            
            # Calculate max funding
            max_from_program = pos_config.get("max_funding", 0)
            max_from_revenue = app.true_monthly_revenue * calc.get("max_advance_percent_of_true_revenue", 1.3)
            
            # Apply gross reserve limit
            gross_reserve = pos_config.get("max_gross_reserve", 0.30)
            available_revenue = app.true_monthly_revenue - app.total_mca_payments_monthly
            max_from_reserve = (available_revenue * gross_reserve) * pos_config.get("max_term_months", 6)
            
            max_funding = min(max_from_program, max_from_revenue, max_from_reserve)
            max_funding = max(0, max_funding)
            
            if max_funding < 5000:  # Minimum viable funding
                continue
            
            # Get buy rate for max term
            max_term = pos_config.get("max_term_months", 6)
            buy_rates = prog.get("buy_rates", {}).get(str(position), {})
            
            # Find appropriate term and rate
            best_term = None
            buy_rate = None
            for term_str in sorted(buy_rates.keys(), key=lambda x: float(x), reverse=True):
                term = float(term_str)
                if term <= max_term:
                    best_term = term
                    buy_rate = buy_rates[term_str]
                    break
            
            if not buy_rate:
                continue
            
            # Calculate sell rate (buy rate + upsell)
            # Default to mid-tier commission
            commission_tiers = self.config.get("commission_tiers", {}).get(prog_key, [])
            mid_tier = commission_tiers[len(commission_tiers) // 2] if commission_tiers else {"upsell": 0.02, "commission": 0.05}
            
            upsell = mid_tier.get("upsell", 0)
            commission_percent = mid_tier.get("commission", 0.05)
            sell_rate = buy_rate + upsell + commission_percent
            
            # Calculate payments
            payback_amount = max_funding * sell_rate
            term_days = int(best_term * 30)
            daily_payment = payback_amount / term_days
            weekly_payment = daily_payment * 5  # 5 business days
            
            # Origination fee
            orig_fee_percent = prog.get("origination_fee", 0.05)
            orig_fee_amount = max_funding * orig_fee_percent
            
            # Commission
            commission_amount = max_funding * commission_percent
            
            offer = ProgramOffer(
                program_name=prog.get("name", prog_key),
                position=position,
                max_funding=round(max_funding, 2),
                max_term_months=best_term,
                buy_rate=buy_rate,
                sell_rate=round(sell_rate, 4),
                daily_payment=round(daily_payment, 2),
                weekly_payment=round(weekly_payment, 2),
                payback_amount=round(payback_amount, 2),
                origination_fee_percent=orig_fee_percent,
                origination_fee_amount=round(orig_fee_amount, 2),
                commission_percent=commission_percent,
                commission_amount=round(commission_amount, 2),
                gross_reserve_percent=gross_reserve
            )
            
            offers.append(offer)
        
        # Sort by funding amount (highest first)
        offers.sort(key=lambda o: o.max_funding, reverse=True)
        
        return offers
    
    def _select_best_offer(self, offers: List[ProgramOffer]) -> ProgramOffer:
        """Select the best offer (lowest rate with highest funding)"""
        if not offers:
            return None
        
        # Sort by: highest funding, then lowest sell rate
        sorted_offers = sorted(offers, key=lambda o: (-o.max_funding, o.sell_rate))
        return sorted_offers[0]
    
    def _get_required_stips(self, funding_amount: float) -> List[str]:
        """Get required stips based on funding amount"""
        stips = self.config.get("stip_requirements", {})
        required = list(stips.get("always_required", []))
        
        if funding_amount > 100000:
            required.extend(stips.get("deals_over_100k", []))
        
        if funding_amount > 150000:
            required.extend(stips.get("deals_over_150k", []))
        
        if funding_amount > 250000:
            required.extend(stips.get("deals_over_250k", []))
        
        if funding_amount > 300000:
            required.extend(stips.get("deals_over_300k", []))
        
        return required
    
    def calculate_offer_with_terms(self, app: ApplicationData, program_key: str, 
                                    position: int, term_months: float, 
                                    commission_percent: float) -> Optional[ProgramOffer]:
        """Calculate a specific offer with custom terms"""
        programs = self.config.get("programs", {})
        prog = programs.get(program_key)
        
        if not prog:
            return None
        
        pos_config = prog.get("positions", {}).get(str(position), {})
        if not pos_config.get("enabled", False):
            return None
        
        # Get buy rate
        buy_rates = prog.get("buy_rates", {}).get(str(position), {})
        buy_rate = None
        
        for term_str, rate in buy_rates.items():
            if float(term_str) >= term_months:
                buy_rate = rate
                break
        
        if not buy_rate:
            return None
        
        # Calculate max funding
        calc = self.config.get("funding_calculation", {})
        max_funding = min(
            pos_config.get("max_funding", 0),
            app.true_monthly_revenue * calc.get("max_advance_percent_of_true_revenue", 1.3)
        )
        
        # Get upsell for commission
        commission_tiers = self.config.get("commission_tiers", {}).get(program_key, [])
        upsell = 0
        for tier in commission_tiers:
            if tier.get("commission", 0) == commission_percent:
                upsell = tier.get("upsell", 0)
                break
        
        sell_rate = buy_rate + upsell + commission_percent
        payback_amount = max_funding * sell_rate
        term_days = int(term_months * 30)
        
        return ProgramOffer(
            program_name=prog.get("name", program_key),
            position=position,
            max_funding=round(max_funding, 2),
            max_term_months=term_months,
            buy_rate=buy_rate,
            sell_rate=round(sell_rate, 4),
            daily_payment=round(payback_amount / term_days, 2),
            weekly_payment=round((payback_amount / term_days) * 5, 2),
            payback_amount=round(payback_amount, 2),
            origination_fee_percent=prog.get("origination_fee", 0.05),
            origination_fee_amount=round(max_funding * prog.get("origination_fee", 0.05), 2),
            commission_percent=commission_percent,
            commission_amount=round(max_funding * commission_percent, 2),
            gross_reserve_percent=pos_config.get("max_gross_reserve", 0.25)
        )


def print_result(result: UnderwritingResult):
    """Pretty print underwriting result"""
    print("\n" + "=" * 70)
    print("UNDERWRITING DECISION")
    print("=" * 70)
    
    # Decision banner
    if result.decision == Decision.AUTO_APPROVE.value:
        print(f"\n✅ DECISION: {result.decision}")
    elif result.decision == Decision.MANUAL_REVIEW.value:
        print(f"\n⚠️  DECISION: {result.decision}")
    else:
        print(f"\n❌ DECISION: {result.decision}")
    
    print(f"   Risk Score: {result.risk_score}/100")
    print(f"   Position: {result.position}")
    
    # Decline reasons
    if result.decline_reasons:
        print(f"\n🚫 DECLINE REASONS:")
        for r in result.decline_reasons:
            print(f"   • {r.message}")
    
    # Flags
    if result.flags:
        print(f"\n⚠️  FLAGS:")
        for f in result.flags:
            print(f"   • {f.message}")
    
    # Eligible programs
    if result.eligible_programs:
        print(f"\n✅ ELIGIBLE PROGRAMS:")
        for p in result.eligible_programs:
            print(f"   • {p}")
    
    # Ineligible programs
    if result.ineligible_programs:
        print(f"\n❌ INELIGIBLE PROGRAMS:")
        for prog, reasons in result.ineligible_programs.items():
            print(f"   {prog}:")
            for r in reasons[:2]:  # Show first 2 reasons
                print(f"      - {r}")
    
    # Offers
    if result.offers:
        print(f"\n💰 AVAILABLE OFFERS:")
        for i, offer in enumerate(result.offers[:3], 1):  # Top 3
            rec = " ⭐ RECOMMENDED" if offer == result.recommended_offer else ""
            print(f"\n   {i}. {offer.program_name} (Position {offer.position}){rec}")
            print(f"      Max Funding:    ${offer.max_funding:>12,.2f}")
            print(f"      Term:           {offer.max_term_months:>12} months")
            print(f"      Buy Rate:       {offer.buy_rate:>12}")
            print(f"      Sell Rate:      {offer.sell_rate:>12}")
            print(f"      Payback:        ${offer.payback_amount:>12,.2f}")
            print(f"      Daily Payment:  ${offer.daily_payment:>12,.2f}")
            print(f"      Commission:     ${offer.commission_amount:>12,.2f} ({offer.commission_percent:.1%})")
    
    # Stips
    if result.stips_required:
        print(f"\n📋 REQUIRED STIPS:")
        for stip in result.stips_required:
            print(f"   • {stip.replace('_', ' ').title()}")
    
    print("\n" + "=" * 70)


def evaluate_from_analysis(analysis_json: dict, app_data: dict = None) -> UnderwritingResult:
    """
    Evaluate an application using MoneyThumb analysis output
    
    Args:
        analysis_json: Output from moneythumb_extractor
        app_data: Additional application data (FICO, TIB, etc.)
    """
    # Extract metrics from analysis
    stats = analysis_json.get("revenue_statistics", {})
    
    # Count MCA positions from detected lenders
    mca_txns = analysis_json.get("mca_transactions", [])
    lenders = set(t.get("lender") for t in mca_txns if t.get("lender"))
    
    # Build application data
    app = ApplicationData(
        monthly_revenue=stats.get("revenue_monthly", 0),
        true_monthly_revenue=stats.get("true_revenue_monthly", 0),
        monthly_expenses=stats.get("expenses_monthly", 0),
        avg_daily_balance=stats.get("combined_avg_daily_balance", 0),
        negative_days=stats.get("days_negative", 0),
        nsf_count=len(analysis_json.get("nsf_transactions", [])),
        mca_withhold_percent=stats.get("mca_withhold_percent", 0),
        total_mca_payments_monthly=stats.get("total_debt_withdrawals", 0) / 2,  # Assume 2 months of data
        existing_positions=len(lenders),
        
        # Estimate deposit days from credit transaction count
        deposit_days_per_month=len(analysis_json.get("true_credit_transactions", [])) // 2
    )
    
    # Override with provided app data
    if app_data:
        for key, value in app_data.items():
            if hasattr(app, key):
                setattr(app, key, value)
    
    # Run evaluation
    engine = UnderwritingEngine()
    return engine.evaluate(app)


if __name__ == "__main__":
    import sys
    
    # Example usage
    if len(sys.argv) > 1:
        # Load analysis JSON
        with open(sys.argv[1], 'r') as f:
            analysis = json.load(f)
        
        # Optional: additional app data from command line or file
        app_data = {
            "fico_score": 620,
            "time_in_business_years": 3,
            "business_name": "Test Business",
            "industry": "retail"
        }
        
        if len(sys.argv) > 2:
            with open(sys.argv[2], 'r') as f:
                app_data = json.load(f)
        
        result = evaluate_from_analysis(analysis, app_data)
        print_result(result)
        
        # Save result
        output_path = Path(sys.argv[1]).stem + "_underwriting.json"
        with open(output_path, 'w') as f:
            json.dump(asdict(result), f, indent=2, default=str)
        print(f"\nSaved: {output_path}")
    
    else:
        print("Usage: python underwriting_engine.py <analysis.json> [app_data.json]")
        print("\nExample:")
        print("  python underwriting_engine.py Application_FA42148_analysis.json")
        
        # Demo with sample data
        print("\n" + "-" * 50)
        print("DEMO: Running with sample data...")
        print("-" * 50)
        
        app = ApplicationData(
            business_name="PRO MECHANICAL SERVICES CO LLC",
            time_in_business_years=5,
            industry="construction",
            fico_score=650,
            monthly_revenue=1867932,
            true_monthly_revenue=1715432,
            monthly_expenses=2125718,
            avg_daily_balance=363920,
            negative_days=0,
            nsf_count=0,
            deposit_days_per_month=47,
            existing_positions=4,  # Intuit, Billd, Cashera, Funders App
            mca_withhold_percent=0.043,
            total_mca_payments_monthly=72988,  # $145,976 / 2 months
            has_contractor_license=True,
            tradelines_count=5
        )
        
        engine = UnderwritingEngine()
        result = engine.evaluate(app)
        print_result(result)
