using System.Diagnostics;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Confluent.Kafka;
using MongoDB.Driver;
using Shop;

var builder=WebApplication.CreateBuilder(args);
Common.Configure(builder,"shop-orders");
builder.Services.AddSingleton(Common.Database("orders").GetCollection<Order>("orders"));
builder.Services.AddHostedService<OutboxPublisher>();
var app=builder.Build();
app.UseExceptionHandler(handler=>handler.Run(async context=>{context.Response.StatusCode=503;await context.Response.WriteAsJsonAsync(new {error="Dependency unavailable",traceId=Activity.Current?.TraceId.ToString()});}));
var orders=app.Services.GetRequiredService<IMongoCollection<Order>>();
await orders.Indexes.CreateOneAsync(new CreateIndexModel<Order>(Builders<Order>.IndexKeys.Ascending(o=>o.IdempotencyKey),new CreateIndexOptions{Unique=true}));
app.MapGet("/health",async()=>new {status="ok",service="shop-orders",outboxPending=await orders.CountDocumentsAsync(o=>!o.OutboxSent)});
app.MapGet("/internal/diagnostics",async()=>{
    var pending=await orders.CountDocumentsAsync(o=>!o.OutboxSent);
    var oldest=await orders.Find(o=>!o.OutboxSent).SortBy(o=>o.CreatedAt).Project(o=>(DateTime?)o.CreatedAt).FirstOrDefaultAsync();
    return Results.Ok(new {service="shop-orders",outboxPending=pending,oldestPendingAt=oldest,oldestPendingAgeSeconds=oldest.HasValue?(double?)Math.Max(0,(DateTime.UtcNow-oldest.Value).TotalSeconds):null,observedAt=DateTime.UtcNow});
});
app.MapPost("/api/orders",async(Checkout checkout,HttpRequest request,IHttpClientFactory factory)=>{
    if(!Pricing.Valid(checkout))return Results.BadRequest(new {error="Invalid checkout"});
    var key=request.Headers["Idempotency-Key"].ToString();
    if(key.Length is <8 or >128)return Results.BadRequest(new {error="Idempotency-Key required (8–128 characters)"});
    var hash=Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(JsonSerializer.Serialize(checkout))));
    var existing=await orders.Find(o=>o.IdempotencyKey==key).FirstOrDefaultAsync();
    if(existing is not null)return existing.RequestHash==hash?Results.Ok(existing):Results.Conflict(new {error="Idempotency key belongs to a different request"});
    var client=factory.CreateClient();client.Timeout=TimeSpan.FromSeconds(10);
    var lines=new List<OrderLine>();
    foreach(var line in checkout.Items){
        var response=await client.GetAsync(Common.Env("CATALOG_URL","http://catalog:8080")+"/api/products/"+Uri.EscapeDataString(line.ProductId));
        if(response.StatusCode==System.Net.HttpStatusCode.NotFound)return Results.BadRequest(new {error="Unknown product"});
        response.EnsureSuccessStatusCode();var product=(await response.Content.ReadFromJsonAsync<Product>())!;
        lines.Add(new(product.Id,product.Name,line.Quantity,product.PriceKopecks));
    }
    var order=new Order{IdempotencyKey=key,RequestHash=hash,Items=lines,CustomerName=checkout.CustomerName,Address=checkout.Address,TotalKopecks=Pricing.Total(lines),TraceParent=Activity.Current?.Id};
    using var span=Common.Activities.StartActivity("mongodb.order.create_with_outbox");
    try{await orders.InsertOneAsync(order);}catch(MongoWriteException e)when(e.WriteError.Category==ServerErrorCategory.DuplicateKey){
        existing=await orders.Find(o=>o.IdempotencyKey==key).FirstOrDefaultAsync();
        return existing.RequestHash==hash?Results.Ok(existing):Results.Conflict();
    }
    app.Logger.LogInformation("Order {OrderId} accepted",order.Id);
    return Results.Created("/api/orders/"+order.Id,order);
});
app.MapGet("/api/orders/{id}",async(string id)=>{var order=await orders.Find(o=>o.Id==id).FirstOrDefaultAsync();return order is null?Results.NotFound():Results.Ok(order);});
app.Run();

class OutboxPublisher(IMongoCollection<Order> orders,ILogger<OutboxPublisher> logger):BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stop)
    {
        using var producer=new ProducerBuilder<string,string>(new ProducerConfig{BootstrapServers=Common.Env("KAFKA_URL","kafka:9092"),EnableIdempotence=true,MessageTimeoutMs=5000}).Build();
        while(!stop.IsCancellationRequested){
            try{
                var pending=await orders.Find(o=>!o.OutboxSent).Limit(20).ToListAsync(stop);
                foreach(var order in pending){
                    ActivityContext.TryParse(order.TraceParent,null,out var parent);
                    using var span=Common.Activities.StartActivity("kafka.orders.publish",ActivityKind.Producer,parent);
                    var headers=new Headers();if(Activity.Current?.Id is {} trace)headers.Add("traceparent",Encoding.UTF8.GetBytes(trace));
                    await producer.ProduceAsync("orders.created",new Message<string,string>{Key=order.Id,Value=JsonSerializer.Serialize(new OrderCreated(order.Id)),Headers=headers},stop);
                    await orders.UpdateOneAsync(o=>o.Id==order.Id,Builders<Order>.Update.Set(o=>o.OutboxSent,true),cancellationToken:stop);
                }
            }catch(Exception e)when(!stop.IsCancellationRequested){logger.LogWarning("Outbox publication failed: {Error}",e.GetType().Name);Common.DependencyErrors.Add(1,new KeyValuePair<string,object?>("dependency","outbox"));}
            await Task.Delay(1000,stop);
        }
    }
}
