package com.example.inventory;

import jakarta.validation.Valid;
import jakarta.ws.rs.Consumes;
import jakarta.ws.rs.DELETE;
import jakarta.ws.rs.DefaultValue;
import jakarta.ws.rs.GET;
import jakarta.ws.rs.HeaderParam;
import jakarta.ws.rs.POST;
import jakarta.ws.rs.PUT;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.QueryParam;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;

import java.util.List;

/** CRUD resource for inventory items. */
@Path("/items")
@Consumes(MediaType.APPLICATION_JSON)
public class ItemResource {

    /** List items in a warehouse. */
    @GET
    public List<Item> list(
            @QueryParam("warehouse") String warehouse,
            @QueryParam("limit") @DefaultValue("50") int limit) {
        return List.of();
    }

    /** Fetch one item. */
    @GET
    @Path("/{id}")
    public Item get(@PathParam("id") long id) {
        throw new ItemNotFoundException(id);
    }

    /** Create an item. */
    @POST
    public Response create(@Valid Item item, @HeaderParam("X-Request-Id") String requestId) {
        return Response.created(null).entity(new Item()).build();
    }

    /** Replace an item. */
    @PUT
    @Path("/{id}")
    public Item update(@PathParam("id") long id, @Valid Item item) {
        return item;
    }

    /** Delete an item. */
    @DELETE
    @Path("/{id}")
    public void delete(@PathParam("id") long id) {
        throw new ItemNotFoundException(id);
    }
}
