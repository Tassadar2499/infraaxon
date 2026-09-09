using System.Diagnostics;
using System.Text.Json;
using Confluent.Kafka;
using MongoDB.Driver;
using Shop;

var builder=WebApplication.CreateBuilder(args);
Common.Configure(builder,"shop-worker");
builder.Services.AddSingleton<WorkerState>();
builder.Services.AddHostedService<Processor>();
var app=builder.Build();
app.MapGet("/health",(WorkerState state)=>new {status="ok",service="shop-worker",lastProcessedAt=state.LastProcessedAt});
app.MapPost("/internal/pause",(HttpRequest request,WorkerState state,int seconds=0)=>{if(!Common.Authorized(request))return Results.Unauthorized();state.PausedUntil=DateTime.UtcNow.AddSeconds(Math.Clamp(seconds,0,300));return Results.Ok();});
app.Run();

class WorkerState{public DateTime PausedUntil{get;set;}public DateTime? LastProcessedAt{get;set;}}
class Processor(WorkerState state,ILogger<Processor> logger):BackgroundService
{
    protected override Task ExecuteAsync(CancellationToken stop)=>Task.Run(async()=>{
        var orders=Common.Database("orders").GetCollection<Order>("orders");
        using var consumer=new ConsumerBuilder<string,string>(new ConsumerConfig{BootstrapServers=Common.Env("KAFKA_URL","kafka:9092"),GroupId="shop-worker",AutoOffsetReset=AutoOffsetReset.Earliest,EnableAutoCommit=false,MaxPollIntervalMs=600000}).Build();
        consumer.Subscribe("orders.created");
        while(!stop.IsCancellationRequested){
            try{
                if(state.PausedUntil>DateTime.UtcNow){await Task.Delay(500,stop);continue;}
                var item=consumer.Consume(TimeSpan.FromSeconds(1));if(item is null)continue;
                ActivityContext.TryParse(Common.TraceParent(item.Message.Headers),null,out var parent);
                using var span=Common.Activities.StartActivity("kafka.orders.process",ActivityKind.Consumer,parent);
                try{
                    var created=JsonSerializer.Deserialize<OrderCreated>(item.Message.Value) ?? throw new InvalidOperationException("Empty event");
                    var order=await orders.Find(o=>o.Id==created.OrderId).FirstOrDefaultAsync(stop);
                    if(order is null)throw new InvalidOperationException("Order not yet available");
                    if(order.Status!="completed"){
                        await orders.UpdateOneAsync(o=>o.Id==created.OrderId,Builders<Order>.Update.Set(o=>o.Status,"processing").Set(o=>o.UpdatedAt,DateTime.UtcNow),cancellationToken:stop);
                        await Task.Delay(1200,stop); // simulated payment and fulfillment; no external financial operation
                        await orders.UpdateOneAsync(o=>o.Id==created.OrderId,Builders<Order>.Update.Set(o=>o.Status,"completed").Set(o=>o.UpdatedAt,DateTime.UtcNow),cancellationToken:stop);
                        Common.CompletedOrders.Add(1);logger.LogInformation("Order {OrderId} completed",created.OrderId);
                    }
                    consumer.Commit(item);state.LastProcessedAt=DateTime.UtcNow;
                }catch(Exception)when(!stop.IsCancellationRequested){consumer.Seek(item.TopicPartitionOffset);throw;}
            }catch(Exception e)when(!stop.IsCancellationRequested){logger.LogWarning("Order processing failed: {Error}",e.GetType().Name);Common.DependencyErrors.Add(1,new KeyValuePair<string,object?>("dependency","processing"));await Task.Delay(1500,stop);}
        }
        consumer.Close();
    },stop);
}
