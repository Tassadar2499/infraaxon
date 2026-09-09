using System.Diagnostics;
using System.Diagnostics.Metrics;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using MongoDB.Bson.Serialization.Attributes;
using MongoDB.Driver;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace Shop;

public static class Common
{
    public static readonly ActivitySource Activities = new("InfraAxon.Shop");
    public static readonly Meter Meter = new("InfraAxon.Shop");
    public static readonly Counter<long> CacheHits = Meter.CreateCounter<long>("shop.cache.hits");
    public static readonly Counter<long> CacheMisses = Meter.CreateCounter<long>("shop.cache.misses");
    public static readonly Counter<long> CompletedOrders = Meter.CreateCounter<long>("shop.orders.completed");
    public static readonly Counter<long> DependencyErrors = Meter.CreateCounter<long>("shop.dependency.errors");
    public static string Env(string name, string fallback) => Environment.GetEnvironmentVariable(name) ?? fallback;
    public static IMongoDatabase Database(string name)
    {
        var settings = MongoClientSettings.FromConnectionString(Env("MONGO_URL", "mongodb://localhost:27017"));
        settings.ServerSelectionTimeout = TimeSpan.FromSeconds(5);
        settings.ConnectTimeout = TimeSpan.FromSeconds(5);
        settings.SocketTimeout = TimeSpan.FromSeconds(10);
        return new MongoClient(settings).GetDatabase(name);
    }
    public static void Configure(WebApplicationBuilder builder, string service)
    {
        builder.Services.AddHttpClient();
        builder.Logging.AddJsonConsole();
        builder.Services.AddOpenTelemetry().ConfigureResource(r => r.AddService(service))
            .WithTracing(t => t.AddSource("InfraAxon.Shop").AddAspNetCoreInstrumentation().AddHttpClientInstrumentation().AddOtlpExporter())
            .WithMetrics(m => m.AddMeter("InfraAxon.Shop").AddAspNetCoreInstrumentation().AddHttpClientInstrumentation().AddOtlpExporter());
        builder.Logging.AddOpenTelemetry(o => { o.IncludeFormattedMessage = true; o.IncludeScopes = true; o.SetResourceBuilder(ResourceBuilder.CreateDefault().AddService(service)); o.AddOtlpExporter(); });
    }
    public static bool Authorized(Microsoft.AspNetCore.Http.HttpRequest request) =>
        !string.IsNullOrEmpty(Environment.GetEnvironmentVariable("SCENARIO_KEY")) &&
        request.Headers["X-Scenario-Key"] == Environment.GetEnvironmentVariable("SCENARIO_KEY");
    public static string? TraceParent(Confluent.Kafka.Headers? headers) => headers?.LastOrDefault(h=>h.Key=="traceparent") is { } h ? System.Text.Encoding.UTF8.GetString(h.GetValueBytes()) : null;
}

public record Product([property: BsonId] string Id, string Name, string Category, string Description, long PriceKopecks, string ImageKey);
public record CartLine(string ProductId, int Quantity);
public record Checkout(List<CartLine> Items, string CustomerName, string Address);
public record OrderLine(string ProductId, string Name, int Quantity, long UnitPriceKopecks);
public class Order
{
    [BsonId] public string Id { get; set; } = Guid.NewGuid().ToString();
    public string IdempotencyKey { get; set; } = "";
    public string RequestHash { get; set; } = "";
    public List<OrderLine> Items { get; set; } = [];
    public string CustomerName { get; set; } = "";
    public string Address { get; set; } = "";
    public long TotalKopecks { get; set; }
    public string Status { get; set; } = "accepted";
    public bool OutboxSent { get; set; }
    public string? TraceParent { get; set; }
    public DateTime CreatedAt { get; set; } = DateTime.UtcNow;
    public DateTime UpdatedAt { get; set; } = DateTime.UtcNow;
}
public record OrderCreated(string OrderId);
public static class Pricing
{
    public static long Total(IEnumerable<OrderLine> items) => items.Aggregate(0L, (sum, i) => checked(sum + checked(i.UnitPriceKopecks*i.Quantity)));
    public static bool Valid(Checkout checkout) => checkout.Items is {Count: >0 and <=30}
        && checkout.Items.All(i=>i is not null && !string.IsNullOrWhiteSpace(i.ProductId) && i.Quantity is >0 and <=100)
        && !string.IsNullOrWhiteSpace(checkout.CustomerName) && checkout.CustomerName.Length <=120
        && !string.IsNullOrWhiteSpace(checkout.Address) && checkout.Address.Length <=500;
}
