using Shop;
using Xunit;
public class PricingTests
{
    [Fact] public void AmountsUseIntegerKopecks()=>Assert.Equal(398L,Pricing.Total([new("1","Product",2,199)]));
    [Fact] public void OverflowIsRejected()=>Assert.Throws<OverflowException>(()=>Pricing.Total([new("1","Product",2,long.MaxValue)]));
    [Theory] [InlineData(0)] [InlineData(-1)] [InlineData(101)]
    public void InvalidQuantitiesRejected(int quantity)=>Assert.False(Pricing.Valid(new([new("1",quantity)],"Customer","Address")));
    [Fact] public void EmptyCartRejected()=>Assert.False(Pricing.Valid(new([],"Customer","Address")));
    [Fact] public void NullCustomerRejected()=>Assert.False(Pricing.Valid(new([new("1",1)],null!,"Address")));
    [Fact] public void NullLineRejected()=>Assert.False(Pricing.Valid(new([null!],"Customer","Address")));
}
