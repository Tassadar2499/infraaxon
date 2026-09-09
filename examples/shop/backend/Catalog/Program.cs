using System.Text.Json;
using Amazon.S3;
using Amazon.S3.Model;
using MongoDB.Driver;
using Shop;
using StackExchange.Redis;

var builder = WebApplication.CreateBuilder(args);
Common.Configure(builder, "shop-catalog");
var app = builder.Build();
app.UseExceptionHandler(handler => handler.Run(async context => {context.Response.StatusCode=503; await context.Response.WriteAsJsonAsync(new {error="Dependency unavailable", traceId=System.Diagnostics.Activity.Current?.TraceId.ToString()});}));
var products = Common.Database("catalog").GetCollection<Product>("products");
var redis = await ConnectionMultiplexer.ConnectAsync(Common.Env("REDIS_URL", "localhost:6379")+",abortConnect=false,connectTimeout=1000,syncTimeout=2000,asyncTimeout=2000");
var s3 = new AmazonS3Client(Common.Env("MINIO_USER","infraaxon"),Common.Env("MINIO_PASSWORD",""),new AmazonS3Config{ServiceURL=Common.Env("S3_URL","http://localhost:9000"),ForcePathStyle=true});

app.MapGet("/health", () => Results.Ok(new {status="ok",service="shop-catalog"}));
app.MapGet("/api/products", async (string? search, string? category, int? page) => {
    using var activity = Common.Activities.StartActivity("catalog.list");
    var pageNumber = Math.Clamp(page ?? 1,1,1000);
    List<Product>? all = null;
    try { var cached = await redis.GetDatabase().StringGetAsync("catalog:products"); if(cached.HasValue){ all=JsonSerializer.Deserialize<List<Product>>(cached.ToString()); Common.CacheHits.Add(1); } }
    catch(RedisException ex) { app.Logger.LogWarning("Redis cache read failed: {Error}",ex.GetType().Name); Common.DependencyErrors.Add(1,new KeyValuePair<string,object?>("dependency","redis")); }
    if(all is null){
        Common.CacheMisses.Add(1);
        using var mongoSpan = Common.Activities.StartActivity("mongodb.catalog.find");
        all=await products.Find(Builders<Product>.Filter.Empty).ToListAsync();
        try { await redis.GetDatabase().StringSetAsync("catalog:products",JsonSerializer.Serialize(all),TimeSpan.FromSeconds(30)); } catch(RedisException) { }
    }
    var filtered=all.Where(p=>(string.IsNullOrEmpty(search)||p.Name.Contains(search,StringComparison.OrdinalIgnoreCase))&&(string.IsNullOrEmpty(category)||p.Category==category)).ToList();
    return Results.Ok(new {items=filtered.Skip((pageNumber-1)*12).Take(12),total=filtered.Count,page=pageNumber});
});
app.MapGet("/api/products/{id}", async (string id) => {
    using var activity=Common.Activities.StartActivity("mongodb.catalog.product");
    var p=await products.Find(p=>p.Id==id).FirstOrDefaultAsync();
    return p is null?Results.NotFound():Results.Ok(p);
});
app.MapGet("/api/products/{id}/image", async (string id) => {
    using var activity=Common.Activities.StartActivity("s3.product.image");
    var p=await products.Find(p=>p.Id==id).FirstOrDefaultAsync();
    if(p is null)return Results.NotFound();
    using var response=await s3.GetObjectAsync("products",p.ImageKey);
    using var buffer=new MemoryStream(); await response.ResponseStream.CopyToAsync(buffer);
    return Results.File(buffer.ToArray(),"image/svg+xml");
});
app.MapPost("/internal/seed", async (HttpRequest request) => {
    if(!Common.Authorized(request))return Results.Unauthorized();
    try{await s3.PutBucketAsync(new PutBucketRequest{BucketName="products"});}catch(AmazonS3Exception e) when(e.ErrorCode is "BucketAlreadyOwnedByYou" or "BucketAlreadyExists"){}
    var names=new[]{"Arc desk lamp","Cloud headphones","Studio keyboard","Field notebook","Orbit speaker","Mono clock","Terra planter","Frame stand","Wave bottle","Focus mouse","Slate tablet case","Loop cable","Pebble light","Grid organizer","Nord mug","Drift backpack","Beam monitor light","Core charger","Linen desk mat","Fold laptop stand"};
    var colors=new[]{"#e3c9ab","#afc2b3","#b3bdd6","#e0b9a7"};
    for(var i=0;i<names.Length;i++){
        var id=(i+1).ToString(); var category=i%3==0?"Рабочее место":i%3==1?"Электроника":"Аксессуары";
        var p=new Product(id,names[i],category,"Продуманная деталь для повседневных задач. Демонстрационный товар.",149000+i*27500,id+".svg");
        await products.ReplaceOneAsync(x=>x.Id==id,p,new ReplaceOptions{IsUpsert=true});
        var svg=$"<svg xmlns='http://www.w3.org/2000/svg' width='600' height='450' viewBox='0 0 600 450'><rect width='600' height='450' fill='{colors[i%4]}'/><ellipse cx='300' cy='355' rx='125' ry='20' fill='#000' opacity='.08'/><rect x='{200+i%3*8}' y='100' width='200' height='235' rx='{20+i%4*16}' fill='#f8f5ee'/><rect x='225' y='127' width='150' height='165' rx='14' fill='#273934'/><circle cx='300' cy='205' r='{24+i%4*8}' fill='{colors[i%4]}'/><text x='300' y='400' text-anchor='middle' font-family='sans-serif' font-size='16' fill='#35413c'>{names[i]}</text></svg>";
        await s3.PutObjectAsync(new PutObjectRequest{BucketName="products",Key=p.ImageKey,ContentBody=svg,ContentType="image/svg+xml"});
    }
    try{await redis.GetDatabase().KeyDeleteAsync("catalog:products");}catch(RedisException){}
    return Results.Ok(new {count=names.Length});
});
app.Run();
